import io
import re
import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.backends.backend_pdf import PdfPages
from rectpack import newPacker, guillotine

st.set_page_config(page_title="Cut List & Quote Generator", layout="wide")
st.title("🪚 Woodworking Cut List & Quoting Engine")
st.markdown("Upload a Fusion 360 BOM (using `[Material] Label - L x W` naming) or a standard CSV.")

# --------------------------------------------------------------------------------------
# Helper Functions & Parsers
# --------------------------------------------------------------------------------------
def load_and_parse(file):
    name = file.name.lower()
    parsed_parts = []
    parsed_assemblies = []
    top_level_names = {}

    if name.endswith('.csv'):
        content = file.getvalue().decode('utf-8')
        lines = content.splitlines()
        
        if len(lines) > 5 and 'Part Name' in lines[5]:
            df = pd.read_csv(io.StringIO(content), skiprows=5)
            part_pattern = r"\[(?P<mat>[a-zA-Z][a-zA-Z0-9_]*|\d+x\d+)\]\s*(?P<label>.*?)\s*(?:-)?\s*(?P<L>\d+\.?\d*)\s*(?:x\s*(?P<W>\d+\.?\d*))?\s*$"
            assembly_pattern = r"\[(?P<hours>\d+(?:\.\d+)?)\]\s*(?P<label>.*)"
            idx_col = df.columns[0]
            abs_qtys = {}

            for _, row in df.iterrows():
                item_no = str(row[idx_col]).strip()
                if item_no == 'nan' or not item_no: continue
                qty = float(row['Quantity']) if pd.notna(row['Quantity']) else 1.0
                
                if '.' in item_no:
                    parent_no = item_no.rsplit('.', 1)[0]
                    abs_qty = abs_qtys.get(parent_no, 1.0) * qty
                else:
                    abs_qty = qty
                abs_qtys[item_no] = abs_qty

                name_val = str(row['Part Name']).strip()
                top_level_id = item_no.split('.')[0]

                if item_no == top_level_id:
                    top_level_names[top_level_id] = name_val

                m_part = re.search(part_pattern, name_val)
                m_asm = re.search(assembly_pattern, name_val)

                if m_part:
                    d = m_part.groupdict()
                    parsed_parts.append({
                        "Build_ID": top_level_id,
                        "Label": d['label'].strip(),
                        "Length": float(d['L']),
                        "Width": float(d['W']) if d['W'] else None,
                        "Quantity": int(abs_qty),
                        "Material": d['mat'].strip()
                    })
                elif m_asm:
                    d = m_asm.groupdict()
                    if item_no == top_level_id:
                        top_level_names[top_level_id] = d['label'].strip()
                    parsed_assemblies.append({
                        "Build_ID": top_level_id,
                        "Label": d['label'].strip(),
                        "Total_Hours": float(d['hours']) * abs_qty,
                        "Quantity": int(abs_qty)
                    })
        else:
            df = pd.read_csv(io.StringIO(content))
            top_level_names['1'] = "Standard Cut List"
            for _, row in df.iterrows():
                parsed_parts.append({
                    "Build_ID": "1", "Label": row['Label'], "Length": float(row['Length']),
                    "Width": float(row['Width']) if pd.notna(row.get('Width')) else None,
                    "Quantity": int(row['Quantity']), "Material": row['Material']
                })
    else:
        df = pd.read_excel(file)
        top_level_names['1'] = "Standard Cut List"
        for _, row in df.iterrows():
            parsed_parts.append({
                "Build_ID": "1", "Label": row['Label'], "Length": float(row['Length']),
                "Width": float(row['Width']) if pd.notna(row.get('Width')) else None,
                "Quantity": int(row['Quantity']), "Material": row['Material']
            })

    df_parts = pd.DataFrame(parsed_parts) if parsed_parts else pd.DataFrame(columns=["Build_ID", "Label", "Length", "Width", "Quantity", "Material"])
    df_asms = pd.DataFrame(parsed_assemblies) if parsed_assemblies else pd.DataFrame(columns=["Build_ID", "Label", "Total_Hours", "Quantity"])
    return df_parts, df_asms, top_level_names

def expand_rows(data):
    expanded = []
    for _, row in data.iterrows():
        for _ in range(int(row["Quantity"])):
            expanded.append(row)
    return pd.DataFrame(expanded)

def pack_1d(expanded_df, kerf, stock_len):
    cuts_1d = []
    oversize = []
    for _, row in expanded_df.iterrows():
        cut_len = float(row["Length"])
        if cut_len > stock_len:
            oversize.append(str(row["Label"]))
        else:
            cuts_1d.append({"label": str(row["Label"]), "length": cut_len})

    cuts_1d = sorted(cuts_1d, key=lambda x: x["length"], reverse=True)
    bins = []
    bin_capacity = stock_len + kerf

    for cut in cuts_1d:
        cut_len_k = cut["length"] + kerf
        placed = False
        for b in bins:
            if b["remaining"] >= cut_len_k:
                b["cuts"].append(cut)
                b["remaining"] -= cut_len_k
                placed = True
                break
        if not placed:
            bins.append({"remaining": bin_capacity - cut_len_k, "cuts": [cut]})

    return bins, oversize

def pack_2d(expanded_sheet, kerf, sheet_l, sheet_w, allow_rotation):
    bin_w, bin_h = sheet_l + kerf, sheet_w + kerf
    packer = newPacker(pack_algo=guillotine.GuillotineBssfSas, rotation=allow_rotation)
    packer.add_bin(bin_w, bin_h, count=max(len(expanded_sheet), 1))

    rid_map = {}
    oversize = []
    rid = 0

    for _, row in expanded_sheet.iterrows():
        w_k, h_k = float(row["Length"]) + kerf, float(row["Width"]) + kerf
        if not (w_k <= bin_w and h_k <= bin_h) and not (allow_rotation and h_k <= bin_w and w_k <= bin_h):
            oversize.append(str(row["Label"]))
            continue

        packer.add_rect(w_k, h_k, rid=rid)
        rid_map[rid] = {"label": str(row["Label"]), "l": float(row["Length"]), "w": float(row["Width"])}
        rid += 1

    packer.pack()
    return list(packer), rid_map, oversize

# --------------------------------------------------------------------------------------
# Application Flow
# --------------------------------------------------------------------------------------
with st.sidebar:
    st.header("Financial Safeguards")
    material_markup = st.number_input("Material Markup (%)", value=35.0, step=5.0, help="Covers gas, hardware, blades, and raw material runs.")
    quote_buffer = st.number_input("Contingency Buffer (%)", value=10.0, step=5.0, help="Extra padding applied to the final quote to absorb unexpected build delays.")
    
    st.divider()
    st.header("Project Settings")
    kerf = st.number_input("Blade Kerf (inches)", value=0.125, step=0.0625, format="%.3f")
    manual_assembly_hours = st.number_input("Additional Manual Assembly (Hours)", value=2.0, step=0.5, help="Adds to the time parsed from the BOM brackets.")
    allow_rotation = st.checkbox("Allow sheet parts to rotate 90°", value=True)
    st.divider()

uploaded_file = st.file_uploader("Upload BOM File", type=["csv", "xlsx", "xls"])

if uploaded_file is not None:
    try:
        df, df_assemblies, top_level_names = load_and_parse(uploaded_file)
        
        if df.empty:
            st.warning("No cuttable parts found. Ensure parts are named like `[2x4] Leg - 84`.")
            st.stop()

        is_sheet = df["Material"].str.lower() == "sheet"
        df.loc[is_sheet, "Material"] = "Sheet"
        df.loc[~is_sheet, "Width"] = df.loc[~is_sheet, "Width"].fillna(0)

        with st.sidebar:
            st.header("Stock Sizing & Pricing")
            mat_settings = {}
            unique_mats = df["Material"].unique()
            
            for mat in unique_mats:
                st.subheader(f"{mat} Settings")
                if mat == "Sheet":
                    mat_settings[mat] = {
                        'l': st.number_input(f"{mat} Length", value=96.0, step=1.0),
                        'w': st.number_input(f"{mat} Width", value=48.0, step=1.0),
                        'price': st.number_input(f"{mat} Cost ($)", value=26.0)
                    }
                else:
                    mat_settings[mat] = {
                        'l': st.number_input(f"{mat} Stock Length", value=96.0, step=12.0),
                        'price': st.number_input(f"{mat} Cost ($)", value=4.50)
                    }

        # Process 1D Lumber
        df_1d = df[~is_sheet]
        all_bins_1d, all_oversize_1d = {}, {}
        
        for mat in df_1d["Material"].unique():
            mat_df = expand_rows(df_1d[df_1d["Material"] == mat])
            bins, oversize = pack_1d(mat_df, kerf, mat_settings[mat]['l'])
            all_bins_1d[mat] = bins
            all_oversize_1d[mat] = oversize

        # Process 2D Sheets
        df_sheet = expand_rows(df[is_sheet])
        sheet_stats, sheet_bins, rid_map, oversize_2d = [], [], {}, []
        if not df_sheet.empty:
            sheet_bins, rid_map, oversize_2d = pack_2d(df_sheet, kerf, mat_settings["Sheet"]['l'], mat_settings["Sheet"]['w'], allow_rotation)
            
            for bin_ in sheet_bins:
                piece_boxes, true_area = [], 0.0
                for rect in bin_:
                    x, y, w, h, rect_id = rect.x, rect.y, rect.width, rect.height, rect.rid
                    actual_w, actual_h = max(0.0, w - kerf), max(0.0, h - kerf)
                    piece_boxes.append({"x": x, "y": y, "w": w, "h": h, "actual_w": actual_w, "actual_h": actual_h, "rect_id": rect_id})
                    true_area += actual_w * actual_h
                sheet_stats.append({"bin": bin_, "piece_boxes": piece_boxes, "true_area": true_area})

        # Pre-calculate Global Metrics
        raw_cost, total_1d_cuts, total_2d_cuts = 0.0, 0, 0
        
        for mat, bins in all_bins_1d.items():
            raw_cost += len(bins) * mat_settings[mat]['price']
            total_1d_cuts += sum(max(0, len(b["cuts"]) - 1) + (1 if b["remaining"] - kerf > 1e-6 else 0) for b in bins)
            
        if sheet_bins:
            raw_cost += len(sheet_bins) * mat_settings["Sheet"]['price']
            total_2d_cuts = sum(len({round(r.x + r.width, 4) for r in s["bin"]} - {mat_settings["Sheet"]['l'] + kerf}) + 
                                len({round(r.y + r.height, 4) for r in s["bin"]} - {mat_settings["Sheet"]['w'] + kerf}) for s in sheet_stats)

        tab_quote, tab_eff, tab_1d, tab_2d = st.tabs(["💰 Quote Generator", "📊 Efficiency & Details", "🌲 1D Cuts", "📐 2D Cuts"])

        # ==============================================================================
        # TAB 1: FINANCIAL QUOTE & BREAKDOWN
        # ==============================================================================
        with tab_quote:
            st.header("💰 Financial Quote Generator")
            
            client_mat_bid = raw_cost * (1 + (material_markup / 100))
            machining_hours = (total_1d_cuts * 30 + total_2d_cuts * 120) / 3600
            bom_hours = df_assemblies["Total_Hours"].sum() if not df_assemblies.empty else 0.0
            total_hours = machining_hours + manual_assembly_hours + bom_hours

            m1, m2, m3 = st.columns(3)
            m1.metric("Raw Lumber Cost", f"${raw_cost:.2f}")
            m2.metric(f"Client Materials ({material_markup}% Markup)", f"${client_mat_bid:.2f}")
            m3.metric("Est. Shop/Build Time", f"{total_hours:.1f} hrs")
            
            st.divider()

            c1, c2 = st.columns(2)
            
            # $30/hr Logic
            with c1:
                st.subheader("Bidding @ $30/hr")
                base_labor_30 = total_hours * 30
                subtotal_30 = client_mat_bid + base_labor_30
                buffer_amount_30 = subtotal_30 * (quote_buffer / 100)
                grand_total_30 = subtotal_30 + buffer_amount_30
                
                st.write(f"**Base Labor:** ${base_labor_30:.2f}")
                st.write(f"**Contingency Buffer ({quote_buffer}%):** +${buffer_amount_30:.2f}")
                st.success(f"**Grand Total: ${grand_total_30:.2f}**")
                
            # $50/hr Logic
            with c2:
                st.subheader("Bidding @ $50/hr")
                base_labor_50 = total_hours * 50
                subtotal_50 = client_mat_bid + base_labor_50
                buffer_amount_50 = subtotal_50 * (quote_buffer / 100)
                grand_total_50 = subtotal_50 + buffer_amount_50
                
                st.write(f"**Base Labor:** ${base_labor_50:.2f}")
                st.write(f"**Contingency Buffer ({quote_buffer}%):** +${buffer_amount_50:.2f}")
                st.success(f"**Grand Total: ${grand_total_50:.2f}**")

            st.divider()
            
            st.header("📋 Project Build Breakdown")
            for bid, b_name in top_level_names.items():
                b_parts = df[df["Build_ID"] == bid]
                b_asms = df_assemblies[df_assemblies["Build_ID"] == bid] if not df_assemblies.empty else pd.DataFrame()
                
                if b_parts.empty and b_asms.empty:
                    continue
                    
                with st.expander(f"🛠️ Build: {b_name}", expanded=True):
                    if not b_asms.empty:
                        b_hours = b_asms["Total_Hours"].sum()
                        st.markdown(f"**⏱️ Total Assembly Time (from brackets):** {b_hours:.2f} hours")
                    if not b_parts.empty:
                        st.dataframe(b_parts[["Label", "Material", "Length", "Width", "Quantity"]], use_container_width=True, hide_index=True)

        # ==============================================================================
        # TAB 2: EFFICIENCY & DETAILS
        # ==============================================================================
        with tab_eff:
            st.header("📊 Project Efficiency & Details")
            e1, e2 = st.columns(2)
            e1.metric("Total Cut Parts", int(df["Quantity"].sum()))
            e2.metric("Total Saw Passes", total_1d_cuts + total_2d_cuts)
            
            st.markdown("#### Material Yield")
            for mat, bins in all_bins_1d.items():
                if not bins: continue
                qty = len(bins)
                st.write(f"- **{mat} Required:** {qty} boards")
                total_used = sum(c["length"] for b in bins for c in b["cuts"])
                total_stock = qty * mat_settings[mat]['l']
                eff = 100 * total_used / total_stock if total_stock else 0
                st.write(f"  ↳ Yield: {eff:.1f}% (Waste: {100-eff:.1f}%)")

            if sheet_stats:
                qty = len(sheet_bins)
                st.write(f"- **Sheet Required:** {qty} panels")
                total_used = sum(s["true_area"] for s in sheet_stats)
                total_stock = qty * (mat_settings["Sheet"]['l'] * mat_settings["Sheet"]['w'])
                eff = 100 * total_used / total_stock if total_stock else 0
                st.write(f"  ↳ Yield: {eff:.1f}% (Waste: {100-eff:.1f}%)")
                
            oversize_count = sum(len(ovs) for ovs in all_oversize_1d.values()) + len(oversize_2d)
            if oversize_count > 0:
                st.divider()
                st.subheader("⚠️ Oversize Warnings")
                for mat, ovs in all_oversize_1d.items():
                    if ovs: st.warning(f"**{mat}** parts exceeding stock length: {', '.join(set(ovs))}")
                if oversize_2d:
                    st.warning(f"**Sheet** parts exceeding stock size: {', '.join(set(oversize_2d))}")

        # ==============================================================================
        # TAB 3: 1D LUMBER DIAGRAMS
        # ==============================================================================
        with tab_1d:
            if any(all_bins_1d.values()):
                st.header("🌲 1D Lumber Cut Diagrams")
                pdf_1d_buf = io.BytesIO()
                with PdfPages(pdf_1d_buf) as pdf_1d:
                    for mat, bins in all_bins_1d.items():
                        if not bins: continue
                        
                        st.subheader(f"{mat} Layouts")
                        num_boards = len(bins)
                        stock_l = mat_settings[mat]['l']
                        
                        fig_1d, ax_1d = plt.subplots(figsize=(10, max(2, num_boards * 0.8)))
                        ax_1d.set_xlim(-5, stock_l + 2)
                        ax_1d.set_ylim(0, num_boards)
                        ax_1d.invert_yaxis()
                        ax_1d.axis('off')
                        ax_1d.set_title(f"{mat} (Stock: {stock_l}\")", fontweight='bold')
                        
                        board_height = 0.6
                        for i, b in enumerate(bins):
                            y_pos = i + 0.2
                            ax_1d.add_patch(patches.Rectangle((0, y_pos), stock_l, board_height, facecolor='#e0e0e0', edgecolor='gray', lw=1))
                            ax_1d.text(-1, y_pos + board_height / 2, f"B{i + 1}", va='center', ha='right', fontsize=10, fontweight='bold')

                            current_x = 0
                            for cut in b["cuts"]:
                                cut_len = cut["length"]
                                ax_1d.add_patch(patches.Rectangle((current_x, y_pos), cut_len, board_height, facecolor='burlywood', edgecolor='saddlebrown', lw=1.5))
                                label_text = f"{cut['label']}\n({cut_len}\")" if cut_len > 12 else f"{cut_len}\""
                                ax_1d.text(current_x + cut_len / 2, y_pos + board_height / 2, label_text, ha='center', va='center', fontsize=8, color='black', fontweight='bold')
                                current_x += cut_len + kerf

                            actual_scrap = max(0.0, b["remaining"] - kerf)
                            if actual_scrap > 3:
                                ax_1d.text(current_x + actual_scrap / 2, y_pos + board_height / 2, f"Scrap:\n{actual_scrap:.1f}\"", ha='center', va='center', fontsize=8, color='#555555', style='italic')

                        st.pyplot(fig_1d)
                        pdf_1d.savefig(fig_1d, bbox_inches="tight")
                        plt.close(fig_1d)
                
                st.download_button("⬇️ Download All 1D Diagrams (PDF)", data=pdf_1d_buf.getvalue(), file_name="Lumber_Cut_Diagrams.pdf", mime="application/pdf")
            else:
                st.info("No 1D lumber parts were found in this upload.")

        # ==============================================================================
        # TAB 4: 2D SHEET DIAGRAMS
        # ==============================================================================
        with tab_2d:
            if sheet_stats:
                st.header("📐 2D Sheet Goods Diagrams")
                cols = st.columns(2)
                sheet_l, sheet_w = mat_settings["Sheet"]['l'], mat_settings["Sheet"]['w']
                
                pdf_buf = io.BytesIO()
                with PdfPages(pdf_buf) as pdf:
                    for i, stat in enumerate(sheet_stats):
                        fig, ax = plt.subplots(figsize=(10, 5))
                        ax.set_xlim(0, sheet_l)
                        ax.set_ylim(0, sheet_w)
                        ax.set_title(f"Sheet {i + 1}")
                        ax.add_patch(patches.Rectangle((0, 0), sheet_l, sheet_w, fill=False, edgecolor='black', lw=3))

                        for box in stat["piece_boxes"]:
                            x, y, w, h, actual_w, actual_h = box["x"], box["y"], box["w"], box["h"], box["actual_w"], box["actual_h"]
                            data = rid_map[box["rect_id"]]

                            ax.add_patch(patches.Rectangle((x, y), w, h, facecolor='#ffcccc', edgecolor='none'))
                            ax.add_patch(patches.Rectangle((x, y), actual_w, actual_h, facecolor='moccasin', edgecolor='saddlebrown', lw=1.5))
                            
                            disp_text = f"{data['label']}\n{data['l']}\" x {data['w']}\"" if abs(actual_w - data["l"]) < 0.001 else f"{data['label']}\n{data['w']}\" x {data['l']}\""
                            ax.text(x + actual_w / 2, y + actual_h / 2, disp_text, ha='center', va='center', fontsize=9, color='black', fontweight='bold')

                        yield_pct = 100 * stat["true_area"] / (sheet_l * sheet_w) if (sheet_l * sheet_w) else 0
                        ax.text(sheet_l - 1, sheet_w - 2, f"Yield: {yield_pct:.1f}%", ha='right', va='top', fontsize=10, fontweight='bold', color='green')

                        pdf.savefig(fig, bbox_inches="tight")
                        with cols[i % 2]:
                            st.pyplot(fig)
                        plt.close(fig)
                        
                st.download_button("⬇️ Download All 2D Diagrams (PDF)", data=pdf_buf.getvalue(), file_name="Sheet_Cut_Diagrams.pdf", mime="application/pdf")
            else:
                st.info("No sheet parts were found in this upload.")

    except Exception as e:
        st.error(f"An error occurred while parsing the file: {e}")
