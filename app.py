import io
import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.backends.backend_pdf import PdfPages
from rectpack import newPacker, guillotine

st.set_page_config(page_title="Cut List Optimizer", layout="wide")
st.title("🪚 Woodworking Cut List Optimizer")
st.markdown("Upload a CSV or Excel file to generate 1D lumber shopping lists and 2D sheet cutting diagrams.")

with st.sidebar:
    st.header("Settings")
    kerf = st.number_input("Blade Kerf (inches)", min_value=0.0, max_value=1.0, value=0.125, step=0.0625, format="%.3f")
    stock_2x4 = st.number_input("2x4 Stock Length (inches)", value=96.0, step=12.0)
    sheet_w = st.number_input("Sheet Width (inches)", value=48.0, step=1.0)
    sheet_l = st.number_input("Sheet Length (inches)", value=96.0, step=1.0)
    allow_rotation = st.checkbox("Allow sheet parts to be rotated 90°", value=True, help="Uncheck if grain direction must be preserved.")

    st.divider()

    sample_df = pd.DataFrame([
        {"Label": "Upright Post", "Length": 72, "Width": 3.5, "Quantity": 6, "Material": "2x4"},
        {"Label": "Bay Rung", "Length": 21, "Width": 3.5, "Quantity": 16, "Material": "2x4"},
        {"Label": "Shelf Deck", "Length": 46.5, "Width": 24, "Quantity": 4, "Material": "Sheet"}
    ])
    buf = io.StringIO()
    sample_df.to_csv(buf, index=False)
    st.download_button("⬇️ Download Sample CSV", data=buf.getvalue().encode(), file_name="sample_cutlist.csv", mime="text/csv")

def expand_rows(data):
    expanded = []
    for _, row in data.iterrows():
        for _ in range(int(row["Quantity"])):
            expanded.append(row)
    return pd.DataFrame(expanded)

def pack_1d(expanded_2x4, kerf, stock_2x4):
    cuts_1d = []
    oversize_1d = []
    for _, row in expanded_2x4.iterrows():
        cut_len = float(row["Length"])
        if cut_len > stock_2x4:
            oversize_1d.append(str(row["Label"]))
        else:
            cuts_1d.append({"label": str(row["Label"]), "length": cut_len})

    cuts_1d = sorted(cuts_1d, key=lambda x: x["length"], reverse=True)
    bins_1d = []
    bin_capacity = stock_2x4 + kerf

    for cut in cuts_1d:
        cut_len_k = cut["length"] + kerf
        placed = False
        for b in bins_1d:
            if b["remaining"] >= cut_len_k:
                b["cuts"].append(cut)
                b["remaining"] -= cut_len_k
                placed = True
                break
        if not placed:
            bins_1d.append({"remaining": bin_capacity - cut_len_k, "cuts": [cut]})

    return bins_1d, oversize_1d

def board_actual_scrap(board, kerf):
    return max(0.0, board["remaining"] - kerf)

def board_cuts_needed(board, kerf):
    n = len(board["cuts"])
    internal = max(0, n - 1)
    final_trim = 1 if board["remaining"] > 1e-6 else 0
    return internal + final_trim

def pack_2d(expanded_sheet, kerf, sheet_l, sheet_w, allow_rotation):
    bin_w, bin_h = sheet_l + kerf, sheet_w + kerf
    packer = newPacker(pack_algo=guillotine.GuillotineBssfSas, rotation=allow_rotation)
    n_pieces_total = len(expanded_sheet)
    packer.add_bin(bin_w, bin_h, count=max(n_pieces_total, 1))

    rid_map = {}
    oversize_2d = []
    rid = 0

    for _, row in expanded_sheet.iterrows():
        w_k = float(row["Length"]) + kerf
        h_k = float(row["Width"]) + kerf

        fits_normal = w_k <= bin_w and h_k <= bin_h
        fits_rotated = h_k <= bin_w and w_k <= bin_h

        if not fits_normal and not (allow_rotation and fits_rotated):
            oversize_2d.append(str(row["Label"]))
            continue

        packer.add_rect(w_k, h_k, rid=rid)
        rid_map[rid] = {"label": str(row["Label"]), "l": float(row["Length"]), "w": float(row["Width"])}
        rid += 1

    packer.pack()
    bins_used = list(packer)
    return bins_used, rid_map, oversize_2d

def sheet_cut_lines(bin_, kerf, sheet_l, sheet_w):
    piece_boxes = []
    true_area_used = 0.0

    for rect in bin_:
        x, y, w, h, rect_id = rect.x, rect.y, rect.width, rect.height, rect.rid
        actual_w = max(0.0, w - kerf)
        actual_h = max(0.0, h - kerf)
        
        piece_boxes.append({"x": x, "y": y, "w": w, "h": h, "actual_w": actual_w, "actual_h": actual_h, "rect_id": rect_id})
        true_area_used += actual_w * actual_h

    return piece_boxes, true_area_used

uploaded_file = st.file_uploader("Upload Cut List (CSV or Excel)", type=["csv", "xlsx", "xls"])

if uploaded_file is not None:
    try:
        if uploaded_file.name.endswith('.csv'):
            df = pd.read_csv(uploaded_file)
        else:
            df = pd.read_excel(uploaded_file)

        df.columns = df.columns.str.strip()
        required_cols = ["Label", "Length", "Width", "Quantity", "Material"]

        if not all(col in df.columns for col in required_cols):
            st.error(f"Error: Your file must contain exactly these columns: {', '.join(required_cols)}")
            st.stop()

        numeric_issues = []
        for col in ["Length", "Width", "Quantity"]:
            coerced = pd.to_numeric(df[col], errors="coerce")
            if coerced.isnull().any():
                bad_labels = df.loc[coerced.isnull(), "Label"].astype(str).tolist()
                numeric_issues.append(f"'{col}' has non-numeric or blank values in row(s): {', '.join(bad_labels)}")
            df[col] = coerced
        if numeric_issues:
            st.error("Please fix the following in your file before continuing:\n\n- " + "\n- ".join(numeric_issues))
            st.stop()

        if (df["Quantity"] <= 0).any():
            st.error("Quantity must be greater than 0 for all rows.")
            st.stop()

        df["Material"] = df["Material"].astype(str).str.strip()
        material_lower = df["Material"].str.lower()

        valid_mask = material_lower.isin(["2x4", "sheet"])
        if not valid_mask.all():
            bad_rows = df.loc[~valid_mask]
            bad_summary = ", ".join(f"{r.Label} ('{r.Material}')" for r in bad_rows.itertuples())
            st.warning(f"⚠️ {len(bad_rows)} row(s) have an invalid Material value and were excluded: {bad_summary}")

        df_2x4 = df[valid_mask & (material_lower == "2x4")]
        df_sheet = df[valid_mask & (material_lower == "sheet")]

        expanded_2x4 = expand_rows(df_2x4) if not df_2x4.empty else pd.DataFrame()
        expanded_sheet = expand_rows(df_sheet) if not df_sheet.empty else pd.DataFrame()

        bins_1d, oversize_1d = ([], [])
        if not expanded_2x4.empty:
            bins_1d, oversize_1d = pack_1d(expanded_2x4, kerf, stock_2x4)

        sheet_bins, rid_map, oversize_2d = ([], {}, [])
        if not expanded_sheet.empty:
            sheet_bins, rid_map, oversize_2d = pack_2d(expanded_sheet, kerf, sheet_l, sheet_w, allow_rotation)

        sheet_stats = []
        for bin_ in sheet_bins:
            piece_boxes, true_area = sheet_cut_lines(bin_, kerf, sheet_l, sheet_w)
            sheet_stats.append({
                "bin": bin_, "piece_boxes": piece_boxes, "true_area": true_area,
            })

        st.header("📋 Project Summary")
        parts_summary = (
            df[valid_mask]
            .groupby(["Label", "Material"], as_index=False)
            .agg(Quantity=("Quantity", "sum"), Length=("Length", "first"), Width=("Width", "first"))
            .sort_values(["Material", "Label"])
        )
        with st.expander("🧾 Full Parts List Needed", expanded=True):
            st.dataframe(parts_summary, use_container_width=True, hide_index=True)

        c1, c2, c3 = st.columns(3)
        c1.metric("96\" 2x4 Boards Needed" if stock_2x4 == 96 else f"{stock_2x4:.0f}\" 2x4 Boards Needed", len(bins_1d))
        c2.metric(f"{sheet_l:.0f}\"x{sheet_w:.0f}\" Sheets Needed", len(sheet_bins))
        c3.metric("Total Parts", int(df.loc[valid_mask, "Quantity"].sum()))

        st.markdown("#### Efficiency & Waste")
        e1, e2, e3, e4 = st.columns(4)
        if bins_1d:
            total_1d_used = sum(c["length"] for b in bins_1d for c in b["cuts"])
            total_1d_stock = len(bins_1d) * stock_2x4
            eff_1d = 100 * total_1d_used / total_1d_stock if total_1d_stock else 0
            e1.metric("2x4 Efficiency", f"{eff_1d:.1f}%")
            e2.metric("2x4 Waste", f"{100 - eff_1d:.1f}%")
        else:
            e1.metric("2x4 Efficiency", "—")
            e2.metric("2x4 Waste", "—")

        if sheet_stats:
            total_sheet_used = sum(s["true_area"] for s in sheet_stats)
            total_sheet_stock = len(sheet_stats) * (sheet_l * sheet_w)
            eff_2d = 100 * total_sheet_used / total_sheet_stock if total_sheet_stock else 0
            e3.metric("Sheet Efficiency", f"{eff_2d:.1f}%")
            e4.metric("Sheet Waste", f"{100 - eff_2d:.1f}%")
        else:
            e3.metric("Sheet Efficiency", "—")
            e4.metric("Sheet Waste", "—")

        if oversize_1d:
            st.warning(f"Oversize Warning: The following 2x4 parts exceed the {stock_2x4}\" stock length: {', '.join(set(oversize_1d))}")
        if oversize_2d:
            st.warning(f"Oversize Warning: The following parts do not fit on a {sheet_l}x{sheet_w} sheet: {', '.join(set(oversize_2d))}")

        st.divider()

        if bins_1d:
            st.header("🌲 1D Lumber (2x4) Cut List")
            num_boards = len(bins_1d)
            fig_1d, ax_1d = plt.subplots(figsize=(10, max(2, num_boards * 0.8)))
            ax_1d.set_xlim(-5, stock_2x4 + 2)
            ax_1d.set_ylim(0, num_boards)
            ax_1d.invert_yaxis()
            ax_1d.axis('off')

            board_height = 0.6
            for i, b in enumerate(bins_1d):
                y_pos = i + 0.2
                ax_1d.add_patch(patches.Rectangle((0, y_pos), stock_2x4, board_height, facecolor='#e0e0e0', edgecolor='gray', lw=1))
                ax_1d.text(-1, y_pos + board_height / 2, f"B{i + 1}", va='center', ha='right', fontsize=10, fontweight='bold')

                current_x = 0
                for cut in b["cuts"]:
                    cut_len = cut["length"]
                    ax_1d.add_patch(patches.Rectangle((current_x, y_pos), cut_len, board_height, facecolor='burlywood', edgecolor='saddlebrown', lw=1.5))
                    label_text = f"{cut['label']}\n({cut_len}\")" if cut_len > 12 else f"{cut_len}\""
                    ax_1d.text(current_x + cut_len / 2, y_pos + board_height / 2, label_text, ha='center', va='center', fontsize=8, color='black', fontweight='bold')
                    current_x += cut_len + kerf

                actual_scrap = board_actual_scrap(b, kerf)
                if actual_scrap > 3:
                    ax_1d.text(current_x + actual_scrap / 2, y_pos + board_height / 2, f"Scrap:\n{actual_scrap:.1f}\"", ha='center', va='center', fontsize=8, color='#555555', style='italic')

            st.pyplot(fig_1d)
            
            buf_1d = io.BytesIO()
            fig_1d.savefig(buf_1d, format="pdf", bbox_inches="tight")
            st.download_button("⬇️ Download 1D Diagrams (PDF)", data=buf_1d.getvalue(), file_name="2x4_Cut_Diagrams.pdf", mime="application/pdf")
            
            plt.close(fig_1d)
            st.divider()

        if sheet_stats:
            st.header("📐 2D Sheet Goods Diagrams")
            cols = st.columns(2)
            
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

                        # 1. Draw the Kerf Waste (Saw Blade Path) in light red
                        ax.add_patch(patches.Rectangle((x, y), w, h, facecolor='#ffcccc', edgecolor='none'))
                        
                        # 2. Draw the Physical Board on top
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
            
        elif not expanded_sheet.empty:
            st.error("Could not pack the sheets. Check if pieces exceed sheet dimensions.")

    except Exception as e:
        st.error(f"An error occurred: {e}")