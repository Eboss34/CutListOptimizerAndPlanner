import datetime
import io
import re
import json
import base64
import requests
import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.backends.backend_pdf import PdfPages
from rectpack import newPacker, guillotine

st.set_page_config(page_title="Cut List & Quote Generator", layout="wide")
st.title("🪚 Woodworking Cut List & Quoting Engine")
st.markdown("Upload a Fusion 360 BOM, or load a past project from the Active Projects tab.")

# --------------------------------------------------------------------------------------
# Helper Functions & Parsers
# --------------------------------------------------------------------------------------
def load_and_parse(file_bytes, file_name):
    name = file_name.lower()
    parsed_parts, parsed_assemblies, top_level_names = [], [], {}
    
    try:
        if name.endswith('.csv'):
            content = file_bytes.decode('utf-8')
            lines = content.splitlines()
            if len(lines) > 5 and 'Part Name' in lines[5]:
                df = pd.read_csv(io.StringIO(content), skiprows=5)
            else:
                df = pd.read_csv(io.StringIO(content))
        else:
            df = pd.read_excel(io.BytesIO(file_bytes))
            if 'Part Name' not in df.columns:
                df = pd.read_excel(io.BytesIO(file_bytes), skiprows=5)
    except Exception as e:
        st.error(f"Error reading file: {e}")
        return pd.DataFrame(), pd.DataFrame(), {}

    if 'Part Name' in df.columns:
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
            if item_no == top_level_id: top_level_names[top_level_id] = name_val

            m_part, m_asm = re.search(part_pattern, name_val), re.search(assembly_pattern, name_val)
            if m_part:
                d = m_part.groupdict()
                parsed_parts.append({"Build_ID": top_level_id, "Label": d['label'].strip(), "Length": float(d['L']), "Width": float(d['W']) if d['W'] else None, "Quantity": int(abs_qty), "Material": d['mat'].strip()})
            elif m_asm:
                d = m_asm.groupdict()
                if item_no == top_level_id: top_level_names[top_level_id] = d['label'].strip()
                parsed_assemblies.append({"Build_ID": top_level_id, "Label": d['label'].strip(), "Total_Hours": float(d['hours']) * abs_qty, "Quantity": int(abs_qty)})
    else:
        top_level_names['1'] = "Standard Cut List"
        for _, row in df.iterrows():
            try:
                parsed_parts.append({"Build_ID": "1", "Label": str(row.get('Label', 'Unnamed')), "Length": float(row['Length']), "Width": float(row['Width']) if pd.notna(row.get('Width')) else None, "Quantity": int(row['Quantity']), "Material": str(row['Material'])})
            except KeyError: pass

    df_parts = pd.DataFrame(parsed_parts) if parsed_parts else pd.DataFrame(columns=["Build_ID", "Label", "Length", "Width", "Quantity", "Material"])
    df_asms = pd.DataFrame(parsed_assemblies) if parsed_assemblies else pd.DataFrame(columns=["Build_ID", "Label", "Total_Hours", "Quantity"])
    return df_parts, df_asms, top_level_names

def expand_rows(data):
    expanded = []
    for _, row in data.iterrows():
        for _ in range(int(row["Quantity"])): expanded.append(row)
    return pd.DataFrame(expanded)

def pack_1d(expanded_df, kerf, stock_len):
    cuts_1d, oversize = [], []
    for _, row in expanded_df.iterrows():
        cut_len = float(row["Length"])
        if cut_len > stock_len: oversize.append(str(row["Label"]))
        else: cuts_1d.append({"label": str(row["Label"]), "length": cut_len})
    
    cuts_1d = sorted(cuts_1d, key=lambda x: x["length"], reverse=True)
    bins = []
    for cut in cuts_1d:
        placed, cut_len_k = False, cut["length"] + kerf
        for b in bins:
            if b["remaining"] >= cut_len_k:
                b["cuts"].append(cut); b["remaining"] -= cut_len_k; placed = True; break
        if not placed: bins.append({"remaining": (stock_len + kerf) - cut_len_k, "cuts": [cut]})
    return bins, oversize

def pack_2d(expanded_sheet, kerf, sheet_l, sheet_w, allow_rotation):
    bin_w, bin_h = sheet_l + kerf, sheet_w + kerf
    packer = newPacker(pack_algo=guillotine.GuillotineBssfSas, rotation=allow_rotation)
    packer.add_bin(bin_w, bin_h, count=max(len(expanded_sheet), 1))
    
    rid_map, oversize, rid = {}, [], 0
    for _, row in expanded_sheet.iterrows():
        w_k, h_k = float(row["Length"]) + kerf, float(row["Width"]) + kerf
        if not (w_k <= bin_w and h_k <= bin_h) and not (allow_rotation and h_k <= bin_w and w_k <= bin_h):
            oversize.append(str(row["Label"])); continue
        packer.add_rect(w_k, h_k, rid=rid)
        rid_map[rid] = {"label": str(row["Label"]), "l": float(row["Length"]), "w": float(row["Width"])}
        rid += 1
    
    packer.pack()
    return list(packer), rid_map, oversize

# Webhook Handlers
def trigger_google_apps_script(webhook_url, payload):
    try:
        res = requests.post(webhook_url, json=payload, allow_redirects=False)
        if res.status_code in (302, 303, 307, 308):
            res = requests.get(res.headers.get('Location'))
        res.raise_for_status()
        return res.json()
    except Exception as e: return {"status": "error", "message": str(e)}

def fetch_ledger_data(webhook_url, file_id=None):
    try:
        url = f"{webhook_url}?action=getFile&fileId={file_id}" if file_id else webhook_url
        res = requests.get(url, allow_redirects=False)
        if res.status_code in (302, 303, 307, 308):
            redirect_url = res.headers.get('Location')
            res = requests.get(redirect_url)
        res.raise_for_status()
        return res.json()
    except Exception as e: 
        return {"status": "error", "message": f"Network Error: {str(e)}"}

# --------------------------------------------------------------------------------------
# Session State Initialization (Memory)
# --------------------------------------------------------------------------------------
if 'mat_markup' not in st.session_state: st.session_state.mat_markup = 25.0
if 'contingency' not in st.session_state: st.session_state.contingency = 10.0
if 'labor_rate' not in st.session_state: st.session_state.labor_rate = 30.0
if 'cleanout' not in st.session_state: st.session_state.cleanout = 0.0
if 'discount' not in st.session_state: st.session_state.discount = 0.0
if 'kerf' not in st.session_state: st.session_state.kerf = 0.125
if 'manual_hrs' not in st.session_state: st.session_state.manual_hrs = 2.0
if 'custom_df' not in st.session_state: st.session_state.custom_df = pd.DataFrame(columns=["Item", "Cost"])
if 'bom_bytes' not in st.session_state: st.session_state.bom_bytes = None
if 'bom_name' not in st.session_state: st.session_state.bom_name = None

# --------------------------------------------------------------------------------------
# Sidebar & Application Flow
# --------------------------------------------------------------------------------------
with st.sidebar:
    st.header("Financial Safeguards")
    mat_markup = st.number_input("Material Markup (%)", value=st.session_state.mat_markup, step=5.0)
    contingency = st.number_input("Unmeasured Consumables (%)", value=st.session_state.contingency, step=5.0)
    labor_rate = st.number_input("Machining & Labor Rate ($/hr)", value=st.session_state.labor_rate, step=5.0)
    
    st.divider()
    st.header("Discounts & Add-Ons")
    cleanout = st.number_input("Guided Clean-Out (Hours)", value=st.session_state.cleanout, step=1.0)
    discount = st.number_input("Global Discount (%)", value=st.session_state.discount, step=1.0)
    
    st.markdown("**Custom Line Items (Stain, Hooks, etc.)**")
    edited_custom = st.data_editor(st.session_state.custom_df, num_rows="dynamic", hide_index=True, use_container_width=True)
    edited_custom["Cost"] = pd.to_numeric(edited_custom["Cost"], errors='coerce').fillna(0)
    custom_items_total = edited_custom["Cost"].sum()
    
    st.divider()
    st.header("Project Settings")
    kerf = st.number_input("Blade Kerf (inches)", value=st.session_state.kerf, step=0.0625, format="%.3f")
    manual_hrs = st.number_input("Additional Manual Assembly (Hrs)", value=st.session_state.manual_hrs, step=0.5)
    allow_rotation = st.checkbox("Allow sheet parts to rotate 90°", value=True)
    
    st.divider()
    st.header("Integrations")
    try:
        apps_script_url = st.secrets["APPS_SCRIPT_URL"]
        st.success("✅ Google Apps Script Connected")
    except Exception:
        st.error("⚠️ APPS_SCRIPT_URL missing in secrets")
        apps_script_url = ""
        
    st.divider()
    uploaded_file = st.file_uploader("Upload BOM File", type=["csv", "xlsx", "xls"])
    if uploaded_file:
        st.session_state.bom_bytes = uploaded_file.getvalue()
        st.session_state.bom_name = uploaded_file.name

# 1. Define Tabs GLOBALLY so they always exist
tab_ledger, tab_quote, tab_docs, tab_eff, tab_1d, tab_2d = st.tabs([
    "📂 Active Projects", "💰 Quote Generator", "📄 Document Gen", 
    "📊 Efficiency", "🌲 1D Cuts", "📐 2D Cuts"
])

# ==============================================================================
# TAB 1: ACTIVE PROJECTS (LOAD CAPABILITY)
# ==============================================================================
with tab_ledger:
    st.header("📂 Active Projects & Ledger")
    st.markdown("Fetch real-time data from your Google Sheet to load past quotes and files.")
    
    if st.button("🔄 Refresh Ledger Data") and apps_script_url:
        with st.spinner("Fetching data from Google Sheets..."):
            res = fetch_ledger_data(apps_script_url)
            if res.get("status") == "success":
                st.session_state.ledger_data = res.get("data", [])
            else: 
                st.error(res.get("message"))
            
    if 'ledger_data' in st.session_state and st.session_state.ledger_data:
        df_ledger = pd.DataFrame(st.session_state.ledger_data)
        st.dataframe(df_ledger, use_container_width=True)
        
        st.divider()
        st.subheader("📥 Load Past Project")
        
        # Ensure the column exists to prevent KeyError
        if 'BOM File ID' in df_ledger.columns:
            valid_projects = df_ledger[df_ledger['BOM File ID'] != ""]
            if not valid_projects.empty:
                load_target = st.selectbox("Select Project to Load:", valid_projects['Client'] + " - " + valid_projects['Project'])
                if st.button("Load Project Data & BOM"):
                    target_row = valid_projects[valid_projects['Client'] + " - " + valid_projects['Project'] == load_target].iloc[0]
                    with st.spinner("Downloading BOM and Restoring Settings..."):
                        
                        # 1. Restore Settings to memory safely
                        try:
                            settings_str = target_row.get('Settings JSON', '{}')
                            if pd.isna(settings_str) or not str(settings_str).strip():
                                settings_str = '{}'
                                
                            saved_settings = json.loads(str(settings_str))
                            st.session_state.mat_markup = float(saved_settings.get('mat_markup', 25.0))
                            st.session_state.contingency = float(saved_settings.get('contingency', 10.0))
                            st.session_state.labor_rate = float(saved_settings.get('labor_rate', 30.0))
                            st.session_state.cleanout = float(saved_settings.get('cleanout', 0.0))
                            st.session_state.discount = float(saved_settings.get('discount', 0.0))
                            st.session_state.kerf = float(saved_settings.get('kerf', 0.125))
                            st.session_state.manual_hrs = float(saved_settings.get('manual_hrs', 2.0))
                            if 'custom_df' in saved_settings:
                                st.session_state.custom_df = pd.DataFrame(saved_settings['custom_df'])
                        except Exception as e: 
                            st.warning(f"Note: Could not restore previous slider settings (Using defaults). Reason: {e}")
                        
                        # 2. Fetch BOM file
                        bom_file_id = str(target_row.get('BOM File ID', '')).strip()
                        bom_res = fetch_ledger_data(apps_script_url, file_id=bom_file_id)
                        
                        if bom_res.get("status") == "success":
                            st.session_state.bom_bytes = base64.b64decode(bom_res.get("bom_b64"))
                            st.session_state.bom_name = bom_res.get("bom_name")
                            st.rerun() 
                        else: 
                            st.error(f"Failed to load BOM file. Reason: {bom_res.get('message')}")
            else: 
                st.info("No projects with saved BOM files found in Ledger.")
        else:
            st.error("Column 'BOM File ID' missing from Google Sheet Ledger.")

        # --- ATTACH BOM TO EXISTING PROJECT ---
        st.divider()
        st.subheader("📎 Attach BOM to Existing Project")
        
        if 'BOM File ID' in df_ledger.columns:
            missing_bom_projects = df_ledger[df_ledger['BOM File ID'] == ""]
            
            if not missing_bom_projects.empty:
                attach_target = st.selectbox("Select Project to Update:", missing_bom_projects['Client'] + " - " + missing_bom_projects['Project'], key="attach_target")
                attach_file = st.file_uploader("Upload BOM for this project", type=["csv", "xlsx", "xls"], key="attach_file")
                
                if st.button("Attach BOM & Save to Ledger") and attach_file:
                    target_row = missing_bom_projects[missing_bom_projects['Client'] + " - " + missing_bom_projects['Project'] == attach_target].iloc[0]
                    
                    with st.spinner("Uploading BOM to client folder..."):
                        file_bytes = attach_file.getvalue()
                        payload = {
                            "client_name": target_row['Client'],
                            "project_name": target_row['Project'],
                            "doc_type": "BOM_Only",
                            "bom_b64": base64.b64encode(file_bytes).decode('utf-8'),
                            "bom_name": attach_file.name,
                            "bom_mime": "text/csv" if attach_file.name.endswith('.csv') else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        }
                        
                        res = trigger_google_apps_script(apps_script_url, payload)
                        if res.get("status") == "success":
                            st.success("✅ BOM Attached Successfully! Click 'Refresh Ledger Data' to update the table.")
                        else:
                            st.error(f"Failed to attach BOM: {res.get('message')}")
            else:
                st.info("All existing projects already have a BOM attached.")

# ==============================================================================
# CORE PROCESSING (Runs if BOM is in session state)
# ==============================================================================
if st.session_state.bom_bytes is not None:
    df, df_assemblies, top_level_names = load_and_parse(st.session_state.bom_bytes, st.session_state.bom_name)
    
    if not df.empty:
        is_sheet = df["Material"].str.lower() == "sheet"
        df.loc[is_sheet, "Material"] = "Sheet"
        df.loc[~is_sheet, "Width"] = df.loc[~is_sheet, "Width"].fillna(0)

        with st.sidebar:
            st.header("Stock Sizing & Pricing")
            mat_settings = {}
            for mat in df["Material"].unique():
                st.subheader(f"{mat} Settings")
                if mat == "Sheet":
                    mat_settings[mat] = {'l': st.number_input(f"{mat} Length", value=96.0), 'w': st.number_input(f"{mat} Width", value=48.0), 'price': st.number_input(f"{mat} Cost ($)", value=26.0)}
                else:
                    mat_settings[mat] = {'l': st.number_input(f"{mat} Stock Length", value=96.0, step=12.0), 'price': st.number_input(f"{mat} Cost ($)", value=4.50)}

        # Pack 1D & 2D
        df_1d = df[~is_sheet]
        all_bins_1d, all_oversize_1d = {}, {}
        for mat in df_1d["Material"].unique():
            bins, oversize = pack_1d(expand_rows(df_1d[df_1d["Material"] == mat]), kerf, mat_settings[mat]['l'])
            all_bins_1d[mat], all_oversize_1d[mat] = bins, oversize

        df_sheet = expand_rows(df[is_sheet])
        sheet_stats, sheet_bins, rid_map, oversize_2d = [], [], {}, []
        if not df_sheet.empty:
            sheet_bins, rid_map, oversize_2d = pack_2d(df_sheet, kerf, mat_settings["Sheet"]['l'], mat_settings["Sheet"]['w'], allow_rotation)
            for bin_ in sheet_bins:
                piece_boxes, true_area = [], 0.0
                for rect in bin_:
                    actual_w, actual_h = max(0.0, rect.width - kerf), max(0.0, rect.height - kerf)
                    label = rid_map[rect.rid]["label"]
                    piece_boxes.append({"x": rect.x, "y": rect.y, "w": rect.width, "h": rect.height, "actual_w": actual_w, "actual_h": actual_h, "rect_id": rect.rid, "label": label})
                    true_area += actual_w * actual_h
                sheet_stats.append({"bin": bin_, "piece_boxes": piece_boxes, "true_area": true_area})
        # Calculations
        raw_cost, total_1d_cuts, total_2d_cuts = 0.0, 0, 0
        for mat, bins in all_bins_1d.items():
            raw_cost += len(bins) * mat_settings[mat]['price']
            total_1d_cuts += sum(max(0, len(b["cuts"]) - 1) + (1 if b["remaining"] - kerf > 1e-6 else 0) for b in bins)
        if sheet_bins:
            raw_cost += len(sheet_bins) * mat_settings["Sheet"]['price']
            total_2d_cuts = sum(len({round(r.x + r.width, 4) for r in s["bin"]} - {mat_settings["Sheet"]['l'] + kerf}) + len({round(r.y + r.height, 4) for r in s["bin"]} - {mat_settings["Sheet"]['w'] + kerf}) for s in sheet_stats)

        client_mat_bid = (raw_cost * (1 + (mat_markup / 100))) + 25.0 
        machining_hours = (total_1d_cuts * 30 + total_2d_cuts * 120) / 3600
        bom_hours = df_assemblies["Total_Hours"].sum() if not df_assemblies.empty else 0.0
        build_hours = machining_hours + manual_hrs + bom_hours
        total_hours = build_hours + cleanout
        
        pre_discount_total = (client_mat_bid + (total_hours * labor_rate) + custom_items_total) * (1 + (contingency / 100))
        discount_amount = pre_discount_total * (discount / 100)
        grand_total = pre_discount_total - discount_amount

        # ==============================================================================
        # TAB 2: FINANCIAL QUOTE
        # ==============================================================================
        with tab_quote:
            st.header(f"💰 Quote: {st.session_state.bom_name}")
            m1, m2, m3 = st.columns(3)
            m1.metric("Raw Lumber Cost", f"${raw_cost:.2f}")
            m2.metric(f"Client Materials (incl $25 fee)", f"${client_mat_bid:.2f}")
            m3.metric("Est. Total Labor Time", f"{total_hours:.1f} hrs")
            
            st.divider()
            st.subheader(f"Bidding @ ${labor_rate}/hr")
            st.write(f"**Build Labor ({build_hours:.1f} hrs):** ${(build_hours * labor_rate):.2f}")
            if cleanout > 0: st.write(f"**Guided Clean-Out ({cleanout:.1f} hrs):** ${(cleanout * labor_rate):.2f}")
            if custom_items_total > 0: st.write(f"**Custom Line Items:** +${custom_items_total:.2f}")
            st.write(f"**Unmeasured Consumables & Contingency ({contingency}%):** +${(pre_discount_total - (pre_discount_total/(1+(contingency/100)))):.2f}")
            if discount > 0: st.write(f"**Discount ({discount}%):** -${discount_amount:.2f}")
            st.success(f"**Fixed Project Investment: ${grand_total:.2f}**")
            
            st.divider()
            st.header("📋 Project Build Breakdown")
            for bid, b_name in top_level_names.items():
                b_parts = df[df["Build_ID"] == bid]
                if not b_parts.empty:
                    with st.expander(f"🛠️ Build: {b_name}", expanded=True):
                        st.dataframe(b_parts[["Label", "Material", "Length", "Width", "Quantity"]], use_container_width=True, hide_index=True)

        # ==============================================================================
        # TAB 3: DOCUMENT GENERATION
        # ==============================================================================
        with tab_docs:
            st.header("📄 Generate Client Documents")
            with st.form("client_doc_form"):
                col1, col2, col3 = st.columns(3)
                client_name = col1.text_input("Client Full Name")
                client_email = col2.text_input("Client Email")
                client_phone = col3.text_input("Client Phone")
                client_address = st.text_input("Installation Address")
                project_name = st.text_input("Project Name (e.g., Garage Wall Shelving)")
                
                scope_default = "Custom heavy-duty modular garage storage utilizing our shared-leg architecture. Includes:\n"
                for b_name in top_level_names.values(): scope_default += f"- {b_name}\n"
                valid_custom_items = [r['Item'] for _, r in edited_custom.iterrows() if pd.notna(r['Item']) and str(r['Item']).strip()]
                if valid_custom_items:
                    scope_default += "\nAdditional Included Materials/Services:\n"
                    for item in valid_custom_items: scope_default += f"- {item}\n"
                    
                project_scope = st.text_area("Project Scope", value=scope_default, height=150)
                
                col_a, col_b = st.columns(2)
                est_start = col_a.date_input("Estimated Start Date")
                est_end = col_b.date_input("Estimated Completion Date")
                
                doc_type = st.selectbox("Document Type", ["Quote/Proposal", "Contract", "Invoice/Receipt"])
                payment_method = st.selectbox("Payment Method", ["Venmo Business", "Credit Card", "ACH / Bank Transfer", "Check"]) if doc_type == "Invoice/Receipt" else "N/A"
                
                submit_doc = st.form_submit_button("🚀 Generate PDF, Save BOM & Update Ledger")
                
                if submit_doc and apps_script_url:
                    with st.spinner(f"Generating {doc_type} and saving files to Drive..."):
                        today = datetime.date.today()
                        doc_number = f"{today.strftime('%Y%m%d')}-{client_name.split()[0].upper()[:4]}" if client_name else f"{today.strftime('%Y%m%d')}-0000"
                        
                        settings_payload = {
                            "mat_markup": mat_markup, "contingency": contingency, "labor_rate": labor_rate,
                            "cleanout": cleanout, "discount": discount, "kerf": kerf, "manual_hrs": manual_hrs,
                            "custom_df": edited_custom.to_dict('records')
                        }
                        
                        payload = {
                            "client_name": client_name, "client_email": client_email, "client_phone": client_phone,
                            "client_address": client_address, "project_name": project_name, "project_scope": project_scope,
                            "doc_type": doc_type, "grand_total": f"${grand_total:,.2f}", "deposit_amount": f"${(grand_total / 2):,.2f}",
                            "balance_amount": f"${(grand_total - (grand_total / 2)):,.2f}", "payment_method": payment_method,
                            "agreement_date": today.strftime("%B %d, %Y"), "payment_date": today.strftime("%B %d, %Y"),
                            "proposal_date": today.strftime("%B %d, %Y"), "valid_until": (today + datetime.timedelta(days=14)).strftime("%B %d, %Y"),
                            "receipt_number": doc_number, "proposal_number": doc_number,
                            "estimated_start": est_start.strftime("%B %d, %Y"), "estimated_completion": est_end.strftime("%B %d, %Y"),
                            "settings_json": json.dumps(settings_payload),
                            "bom_b64": base64.b64encode(st.session_state.bom_bytes).decode('utf-8'),
                            "bom_name": st.session_state.bom_name,
                            "bom_mime": "text/csv" if st.session_state.bom_name.endswith('.csv') else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        }
                        
                        res = trigger_google_apps_script(apps_script_url, payload)
                        if res.get("status") == "success":
                            st.success("✅ Document Created, BOM Saved & Ledger Updated!")
                            st.markdown(f"[🔗 Open {doc_type}]({res.get('pdf_url')}) | [📂 Open Client Folder]({res.get('folder_url')})")
                        else: st.error(f"Failed to generate: {res.get('message')}")

        # ==============================================================================
        # TAB 4, 5, 6: EFFICIENCY & DIAGRAMS
        # ==============================================================================
        with tab_eff:
            st.header("📊 Project Efficiency")
            st.metric("Total Cut Parts", int(df["Quantity"].sum()))
            
            st.divider()
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
            
        with tab_1d:
            if any(all_bins_1d.values()):
                st.header("🌲 1D Lumber Cut Diagrams")
                pdf_1d_buf = io.BytesIO()
                import textwrap  # To handle line breaks in the PDF text
                
                with PdfPages(pdf_1d_buf) as pdf_1d:
                    for mat, bins in all_bins_1d.items():
                        if not bins: continue
                        st.subheader(f"{mat} Layouts")
                        
                        # Generate Key for Labels
                        unique_labels = list({cut["label"] for b in bins for cut in b["cuts"]})
                        label_key = {lbl: str(i+1) for i, lbl in enumerate(unique_labels)}
                        
                        # Display on UI
                        st.markdown(f"**{mat} Part Key:** " + " | ".join([f"**{v}**: {k}" for k, v in label_key.items()]))
                        
                        # Save Key as the first page in the PDF for this material
                        key_fig, key_ax = plt.subplots(figsize=(10, max(1.5, len(unique_labels) * 0.15)))
                        key_ax.axis('off')
                        key_str = f"{mat} Part Key:\n" + " | ".join([f"[{v}] {k}" for k, v in label_key.items()])
                        key_ax.text(0, 0.5, textwrap.fill(key_str, width=100), fontsize=10, va='center', ha='left')
                        pdf_1d.savefig(key_fig, bbox_inches="tight")
                        plt.close(key_fig)
                        
                        # Plot Diagrams
                        num_boards, stock_l = len(bins), mat_settings[mat]['l']
                        fig_1d, ax_1d = plt.subplots(figsize=(10, max(2, num_boards * 0.8)))
                        ax_1d.set_xlim(-5, stock_l + 2); ax_1d.set_ylim(0, num_boards); ax_1d.invert_yaxis(); ax_1d.axis('off')
                        for i, b in enumerate(bins):
                            y_pos = i + 0.2
                            ax_1d.add_patch(patches.Rectangle((0, y_pos), stock_l, 0.6, facecolor='#e0e0e0', edgecolor='gray'))
                            ax_1d.text(-1, y_pos + 0.3, f"B{i + 1}", va='center', ha='right', fontsize=10, fontweight='bold')
                            current_x = 0
                            for cut in b["cuts"]:
                                cut_len = cut["length"]
                                cut_id = label_key[cut["label"]]
                                ax_1d.add_patch(patches.Rectangle((current_x, y_pos), cut_len, 0.6, facecolor='burlywood', edgecolor='saddlebrown'))
                                ax_1d.text(current_x + cut_len / 2, y_pos + 0.3, f"[{cut_id}] {cut_len:g}\"", ha='center', va='center', fontsize=8)
                                current_x += cut_len + kerf
                        st.pyplot(fig_1d)
                        pdf_1d.savefig(fig_1d, bbox_inches="tight")
                        plt.close(fig_1d)
                st.download_button("⬇️ Download 1D Diagrams (PDF)", data=pdf_1d_buf.getvalue(), file_name="Lumber_Cuts.pdf", mime="application/pdf")
            else: st.info("No 1D lumber parts found.")
        with tab_2d:
            if sheet_stats:
                st.header("📐 2D Sheet Goods Diagrams")
                
                # Generate Key for Labels
                unique_labels = list({box["label"] for stat in sheet_stats for box in stat["piece_boxes"]})
                label_key = {lbl: str(i+1) for i, lbl in enumerate(unique_labels)}
                st.markdown("**Part Key:** " + " | ".join([f"**{v}**: {k}" for k, v in label_key.items()]))
                
                cols = st.columns(2)
                sheet_l, sheet_w = mat_settings["Sheet"]['l'], mat_settings["Sheet"]['w']
                pdf_buf = io.BytesIO()
                import textwrap # To handle line breaks in the PDF text
                
                with PdfPages(pdf_buf) as pdf:
                    # Save Key as the first page in the PDF
                    key_fig, key_ax = plt.subplots(figsize=(10, max(1.5, len(unique_labels) * 0.15)))
                    key_ax.axis('off')
                    key_str = "Sheet Goods Part Key:\n" + " | ".join([f"[{v}] {k}" for k, v in label_key.items()])
                    key_ax.text(0, 0.5, textwrap.fill(key_str, width=100), fontsize=10, va='center', ha='left')
                    pdf.savefig(key_fig, bbox_inches="tight")
                    plt.close(key_fig)
                    
                    # Plot Diagrams
                    for i, stat in enumerate(sheet_stats):
                        fig, ax = plt.subplots(figsize=(10, 5))
                        ax.set_xlim(0, sheet_l); ax.set_ylim(0, sheet_w); ax.set_title(f"Sheet {i + 1}")
                        ax.add_patch(patches.Rectangle((0, 0), sheet_l, sheet_w, fill=False, edgecolor='black', lw=3))
                        for box in stat["piece_boxes"]:
                            ax.add_patch(patches.Rectangle((box["x"], box["y"]), box["w"], box["h"], facecolor='#ffcccc', edgecolor='none'))
                            ax.add_patch(patches.Rectangle((box["x"], box["y"]), box["actual_w"], box["actual_h"], facecolor='moccasin', edgecolor='saddlebrown', lw=1.5))
                            
                            cut_id = label_key[box["label"]]
                            cx, cy = box["x"] + box["actual_w"] / 2, box["y"] + box["actual_h"] / 2
                            ax.text(cx, cy, f"[{cut_id}]\n{box['actual_w']:g}\"x{box['actual_h']:g}\"", ha='center', va='center', fontsize=8, color='black')
                            
                        pdf.savefig(fig, bbox_inches="tight")
                        with cols[i % 2]: st.pyplot(fig)
                        plt.close(fig)
                st.download_button("⬇️ Download 2D Diagrams (PDF)", data=pdf_buf.getvalue(), file_name="Sheet_Cuts.pdf", mime="application/pdf")
            else: st.info("No sheet parts found.")
            
else:
    with tab_quote: st.info("Upload a BOM or load a past project to generate a quote.")
    with tab_docs: st.info("Upload a BOM or load a past project to generate client documents.")
    with tab_eff: st.info("Upload a BOM or load a past project to calculate efficiency.")
    with tab_1d: st.info("Upload a BOM or load a past project to view 1D lumber cuts.")
    with tab_2d: st.info("Upload a BOM or load a past project to view 2D sheet cuts.")
