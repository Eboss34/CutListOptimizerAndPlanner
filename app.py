import datetime
import io
import re
import json
import requests
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

    # 1. Load the raw dataframe dynamically
    try:
        if name.endswith('.csv'):
            content = file.getvalue().decode('utf-8')
            lines = content.splitlines()
            if len(lines) > 5 and 'Part Name' in lines[5]:
                df = pd.read_csv(io.StringIO(content), skiprows=5)
            else:
                df = pd.read_csv(io.StringIO(content))
        else:
            df = pd.read_excel(file)
            if 'Part Name' not in df.columns:
                df = pd.read_excel(file, skiprows=5)
                
    except Exception as e:
        st.error(f"Error reading file: {e}")
        return pd.DataFrame(), pd.DataFrame(), {}

    # 2. Route to correct parsing logic based on columns
    if 'Part Name' in df.columns:
        # FUSION 360 BOM PARSER
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
        # STANDARD CUT LIST PARSER
        top_level_names['1'] = "Standard Cut List"
        for _, row in df.iterrows():
            try:
                parsed_parts.append({
                    "Build_ID": "1", 
                    "Label": str(row.get('Label', 'Unnamed')), 
                    "Length": float(row['Length']),
                    "Width": float(row['Width']) if pd.notna(row.get('Width')) else None,
                    "Quantity": int(row['Quantity']), 
                    "Material": str(row['Material'])
                })
            except KeyError:
                pass

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

# Webhook Handlers
def trigger_google_apps_script(webhook_url, payload):
    try:
        # Step 1: Send the data. Google runs doPost() and returns a redirect link to the results.
        res = requests.post(webhook_url, json=payload, allow_redirects=False)
        
        # Step 2: Fetch the generated PDF links from the redirect URL using GET, not POST.
        if res.status_code in (302, 303, 307, 308):
            redirect_url = res.headers.get('Location')
            res = requests.get(redirect_url)
            
        res.raise_for_status()
        return res.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}

def fetch_ledger_data(webhook_url):
    try:
        res = requests.get(webhook_url, allow_redirects=True)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --------------------------------------------------------------------------------------
# Application Flow
# --------------------------------------------------------------------------------------
with st.sidebar:
    st.header("Financial Safeguards")
    material_markup = st.number_input("Material Markup (%)", value=25.0, step=5.0)
    quote_buffer = st.number_input("Unmeasured Consumables & Contingency (%)", value=10.0, step=5.0)
    machining_rate = st.number_input("Machining & Labor Rate ($/hr)", value=30.0, step=5.0)
    
    st.divider()
    st.header("Discounts & Add-Ons")
    cleanout_hours = st.number_input("Guided Clean-Out (Hours)", value=0.0, step=1.0)
    global_discount = st.number_input("Global Discount (%)", value=0.0, step=1.0)
    
    st.markdown("**Custom Line Items (Stain, Hooks, etc.)**")
    custom_df = pd.DataFrame(columns=["Item", "Cost"])
    edited_custom = st.data_editor(custom_df, num_rows="dynamic", hide_index=True, use_container_width=True)
    edited_custom["Cost"] = pd.to_numeric(edited_custom["Cost"], errors='coerce').fillna(0)
    custom_items_total = edited_custom["Cost"].sum()
    
    st.divider()
    st.header("Project Settings")
    kerf = st.number_input("Blade Kerf (inches)", value=0.125, step=0.0625, format="%.3f")
    manual_assembly_hours = st.number_input("Additional Manual Assembly (Hrs)", value=2.0, step=0.5)
    allow_rotation = st.checkbox("Allow sheet parts to rotate 90°", value=True)
    
    st.divider()
    st.header("Integrations")
    try:
        apps_script_url = st.secrets["APPS_SCRIPT_URL"]
        st.success("✅ Google Apps Script Connected")
    except Exception:
        st.error("⚠️ APPS_SCRIPT_URL missing in .streamlit/secrets.toml")
        apps_script_url = ""
        
    st.divider()
    uploaded_file = st.file_uploader("Upload BOM File", type=["csv", "xlsx", "xls"])

# 1. Define Tabs GLOBALLY so they always exist
tab_ledger, tab_quote, tab_docs, tab_eff, tab_1d, tab_2d = st.tabs([
    "📂 Active Projects", "💰 Quote Generator", "📄 Document Gen", 
    "📊 Efficiency", "🌲 1D Cuts", "📐 2D Cuts"
])

# 2. Render the Ledger independently of the BOM upload
with tab_ledger:
    st.header("📂 Active Projects & Ledger")
    st.markdown("Fetch real-time data from your Google Sheet Ledger to track deposits and project statuses.")
    
    if st.button("🔄 Refresh Ledger Data"):
        if not apps_script_url:
            st.error("⚠️️ Cannot fetch data. Apps Script URL missing.")
        else:
            with st.spinner("Fetching data from Google Sheets..."):
                ledger_response = fetch_ledger_data(apps_script_url)
                
                if ledger_response.get("status") == "success":
                    ledger_data = ledger_response.get("data", [])
                    if ledger_data:
                        df_ledger = pd.DataFrame(ledger_data)
                        st.dataframe(df_ledger, use_container_width=True)
                    else:
                        st.info("No active projects found in the ledger yet.")
                else:
                    st.error(f"Error: {ledger_response.get('message')}")

# 3. Process BOM if uploaded
if uploaded_file is not None:
    try:
        df, df_assemblies, top_level_names = load_and_parse(uploaded_file)
        
        if df.empty:
            st.warning("No cuttable parts found.")
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

        # Pack 1D
        df_1d = df[~is_sheet]
        all_bins_1d, all_oversize_1d = {}, {}
        for mat in df_1d["Material"].unique():
            mat_df = expand_rows(df_1d[df_1d["Material"] == mat])
            bins, oversize = pack_1d(mat_df, kerf, mat_settings[mat]['l'])
            all_bins_1d[mat] = bins
            all_oversize_1d[mat] = oversize

        # Pack 2D
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

        # Cost Calculations
        raw_cost, total_1d_cuts, total_2d_cuts = 0.0, 0, 0
        for mat, bins in all_bins_1d.items():
            raw_cost += len(bins) * mat_settings[mat]['price']
            total_1d_cuts += sum(max(0, len(b["cuts"]) - 1) + (1 if b["remaining"] - kerf > 1e-6 else 0) for b in bins)
            
        if sheet_bins:
            raw_cost += len(sheet_bins) * mat_settings["Sheet"]['price']
            total_2d_cuts = sum(len({round(r.x + r.width, 4) for r in s["bin"]} - {mat_settings["Sheet"]['l'] + kerf}) + 
                                len({round(r.y + r.height, 4) for r in s["bin"]} - {mat_settings["Sheet"]['w'] + kerf}) for s in sheet_stats)

        # Base Master Context Variables
        client_mat_bid = (raw_cost * (1 + (material_markup / 100))) + 25.0 
        machining_hours = (total_1d_cuts * 30 + total_2d_cuts * 120) / 3600
        bom_hours = df_assemblies["Total_Hours"].sum() if not df_assemblies.empty else 0.0
        
        # Labor Math
        build_hours = machining_hours + manual_assembly_hours + bom_hours
        total_hours = build_hours + cleanout_hours
        base_labor = total_hours * machining_rate
        
        # Total Math
        subtotal = client_mat_bid + base_labor + custom_items_total
        buffer_amount = subtotal * (quote_buffer / 100)
        pre_discount_total = subtotal + buffer_amount
        
        # Discount Math
        discount_amount = pre_discount_total * (global_discount / 100)
        grand_total = pre_discount_total - discount_amount

        # ==============================================================================
        # TAB 1: FINANCIAL QUOTE
        # ==============================================================================
        with tab_quote:
            st.header("💰 Financial Quote Generator")
            m1, m2, m3 = st.columns(3)
            m1.metric("Raw Lumber Cost", f"${raw_cost:.2f}")
            m2.metric(f"Client Materials (incl $25 fee)", f"${client_mat_bid:.2f}")
            m3.metric("Est. Total Labor Time", f"{total_hours:.1f} hrs")
            
            st.divider()
            st.subheader(f"Bidding @ ${machining_rate}/hr")
            st.write(f"**Build Labor ({build_hours:.1f} hrs):** ${(build_hours * machining_rate):.2f}")
            
            if cleanout_hours > 0:
                st.write(f"**Guided Clean-Out ({cleanout_hours:.1f} hrs):** ${(cleanout_hours * machining_rate):.2f}")
                
            if custom_items_total > 0:
                st.write(f"**Custom Line Items:** +${custom_items_total:.2f}")
                
            st.write(f"**Unmeasured Consumables & Contingency ({quote_buffer}%):** +${buffer_amount:.2f}")
            
            if global_discount > 0:
                st.write(f"**Discount ({global_discount}%):** -${discount_amount:.2f}")
                
            st.success(f"**Fixed Project Investment: ${grand_total:.2f}**")
            
            st.divider()
            st.header("📋 Project Build Breakdown")
            for bid, b_name in top_level_names.items():
                b_parts = df[df["Build_ID"] == bid]
                if not b_parts.empty:
                    with st.expander(f"🛠️ Build: {b_name}", expanded=True):
                        st.dataframe(b_parts[["Label", "Material", "Length", "Width", "Quantity"]], use_container_width=True, hide_index=True)

        # ==============================================================================
        # TAB 2: DOCUMENT GENERATION
        # ==============================================================================
        with tab_docs:
            st.header("📄 Generate Client Documents")
            st.markdown("Fill out the client details below to generate a PDF via Google Docs.")
            
            with st.form("client_doc_form"):
                col1, col2, col3 = st.columns(3)
                client_name = col1.text_input("Client Full Name")
                client_email = col2.text_input("Client Email")
                client_phone = col3.text_input("Client Phone")
                
                client_address = st.text_input("Installation Address")
                project_name = st.text_input("Project Name (e.g., Garage Wall Shelving)")
                
                # Auto-generate a scope summary including custom items
                scope_default = "Custom heavy-duty modular garage storage utilizing our shared-leg architecture. Includes:\n"
                for b_name in top_level_names.values():
                    scope_default += f"- {b_name}\n"
                
                valid_custom_items = [r['Item'] for _, r in edited_custom.iterrows() if pd.notna(r['Item']) and str(r['Item']).strip()]
                if valid_custom_items:
                    scope_default += "\nAdditional Included Materials/Services:\n"
                    for item in valid_custom_items:
                        scope_default += f"- {item}\n"
                    
                project_scope = st.text_area("Project Scope (Appears on Proposal & Contract)", value=scope_default, height=150)
                
                col_a, col_b = st.columns(2)
                est_start = col_a.date_input("Estimated Start Date")
                est_end = col_b.date_input("Estimated Completion Date")
                
                doc_type = st.selectbox("Document Type", ["Quote/Proposal", "Contract", "Invoice/Receipt"])
                
                payment_method = "N/A"
                if doc_type == "Invoice/Receipt":
                    payment_method = st.selectbox("Payment Method", ["Venmo Business", "Credit Card", "ACH / Bank Transfer", "Check"])
                
                submit_doc = st.form_submit_button("🚀 Generate PDF & Update Ledger")
                
                if submit_doc:
                    if not apps_script_url:
                        st.error("⚠️ Please configure APPS_SCRIPT_URL in secrets.")
                    else:
                        with st.spinner(f"Generating {doc_type} for {client_name}..."):
                            today = datetime.date.today()
                            today_str = today.strftime("%B %d, %Y")
                            valid_until_str = (today + datetime.timedelta(days=14)).strftime("%B %d, %Y")
                            
                            doc_number = f"{today.strftime('%Y%m%d')}-{client_name.split()[0].upper()[:4]}" if client_name else f"{today.strftime('%Y%m%d')}-0000"
                            deposit = grand_total / 2
                            balance = grand_total - deposit
                            
                            payload = {
                                "client_name": client_name,
                                "client_email": client_email,
                                "client_phone": client_phone,
                                "client_address": client_address,
                                "project_name": project_name,
                                "project_scope": project_scope,
                                "doc_type": doc_type,
                                "grand_total": f"${grand_total:,.2f}",
                                "deposit_amount": f"${deposit:,.2f}",
                                "balance_amount": f"${balance:,.2f}",
                                "payment_method": payment_method,
                                "agreement_date": today_str,
                                "payment_date": today_str,
                                "proposal_date": today_str,
                                "valid_until": valid_until_str,
                                "receipt_number": doc_number,
                                "proposal_number": doc_number,
                                "estimated_start": est_start.strftime("%B %d, %Y"),
                                "estimated_completion": est_end.strftime("%B %d, %Y")
                            }
                            
                            response = trigger_google_apps_script(apps_script_url, payload)
                            
                            if response.get("status") == "success":
                                st.success(f"✅ Document Created & Ledger Updated!")
                                st.markdown(f"[🔗 Open {doc_type}]({response.get('pdf_url')}) | [📂 Open Client Folder]({response.get('folder_url')})")
                            else:
                                st.error(f"Failed to generate document: {response.get('message')}")

       # ==============================================================================
        # TAB 4: EFFICIENCY
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
        # TAB 5: 1D LUMBER DIAGRAMS
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
        # TAB 6: 2D SHEET DIAGRAMS
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

# If no file is uploaded, show these placeholders in the remaining tabs
else:
    with tab_quote:
        st.info("Upload a BOM file to generate a quote.")
    with tab_docs:
        st.info("Upload a BOM file to generate client documents.")
    with tab_eff:
        st.info("Upload a BOM file to calculate efficiency.")
    with tab_1d:
        st.info("Upload a BOM file to view 1D lumber cuts.")
    with tab_2d:
        st.info("Upload a BOM file to view 2D sheet cuts.")
