import re
import pandas as pd
from rapidfuzz import process, fuzz
from app.sheets_client import SheetsClient

def extract_sheet_id(url):
    """Extracts the unique ID from a Google Sheet URL."""
    match = re.search(r'/d/([a-zA-Z0-9-_]+)', url)
    if match:
        return match.group(1)
    raise ValueError("Invalid Google Sheet URL provided.")

def normalize_name(name):
    import re
    s = str(name).strip().lower()
    s = s.replace("'", "").replace("’", "").replace("-", " ").replace(".", " ")
    return re.sub(r'\s+', ' ', s)

def clean_text(text):
    """Normalize text for matching (lowercase, strip whitespace)."""
    if pd.isna(text):
        return ""
    return str(text).strip().lower()

def normalize_key(text):
    """Normalize text for matching (lowercase, strip whitespace)."""
    if pd.isna(text):
        return ""
    return str(text).strip().lower()

def find_cols(columns, exact_matches, partial_matches=None, exclude_words=None):
    """Robustly find ALL columns based on exact and partial matches."""
    matched = []
    exclude_words = exclude_words or []
    for exact in exact_matches:
        for col in columns:
            if exact == str(col).strip().lower() and col not in matched:
                if not any(ex in str(col).strip().lower() for ex in exclude_words):
                    matched.append(col)
    if partial_matches:
        for partial in partial_matches:
            for col in columns:
                if partial in str(col).strip().lower() and col not in matched:
                    if not any(ex in str(col).strip().lower() for ex in exclude_words):
                        matched.append(col)
    return matched

def get_first_valid(row, cols):
    """Return the first non-empty value from a list of columns."""
    import pandas as pd
    for col in cols:
        val = row.get(col, '')
        if pd.isna(val):
            continue
        val = str(val).strip()
        if val and val.lower() != 'nan':
            return val
    return ''

def process_training_data(sheet_url, csv_filepaths, target_tab_name, workshop_type, log_callback):
    client = SheetsClient(log_callback)
    
    # 1. Extract Sheet ID
    log_callback("Extracting target ID from Google Sheet URL...")
    sheet_id = extract_sheet_id(sheet_url)
    
    # 2. Fetch Session Data
    session_data = client.get_sheet_data(sheet_id)
    session_df = pd.DataFrame(session_data)
    log_callback(f"Successfully read {len(session_df)} rows from Session Sheet.")
    
    calendly_lookup = {}
    calendly_dfs = []
    if csv_filepaths:
        for filepath in csv_filepaths:
            try:
                import os
                cdf = pd.read_csv(filepath)
                calendly_dfs.append(cdf)
                log_callback(f"Read {len(cdf)} rows from CSV ({os.path.basename(filepath)}).")
                
                c_email_cols = find_cols(cdf.columns, ['email', 'email address', 'invitee email'], ['email'])
                c_fname_cols = find_cols(cdf.columns, ['first name', 'given name'])
                c_lname_cols = find_cols(cdf.columns, ['last name', 'surname', 'family name'])
                c_name_cols = find_cols(cdf.columns, ['student name', 'learner name', 'invitee name', 'full name', 'name', 'student'], ['name', 'student'])
                c_school_cols = find_cols(cdf.columns, ['full name of your school', 'school', 'school name', 'organisation', 'organization', 'organisation name', 'organization name', 'employer'], ['school', 'organisation', 'campus', 'employer'], exclude_words=['type', 'best describes', 'postcode', 'device', 'required', 'address'])
                c_school_type_cols = find_cols(cdf.columns, ['school type', 'which best describes your school?'], ['type of school', 'best describes your school', 'school type'])
                c_device_cols = find_cols(cdf.columns, ['device', 'device pack'], ['anapen', 'epipen', 'neffy', 'jext', 'trainer device', 'training equipment', 'device pack', 'devices for your assessment', 'which device'], exclude_words=['dietary', 'wheelchair', 'accommodations'])
                c_event_cols = find_cols(cdf.columns, ['session name', 'class name', 'event name', 'event type name', 'course name', 'course'], ['event type', 'session', 'event'])

                log_callback(f"Calendly CSV extracted columns. Name:{bool(c_name_cols)} School:{bool(c_school_cols)} Device:{bool(c_device_cols)}")
                if not c_device_cols:
                    log_callback(f"WARNING: Could not find Device column in CSV! Columns available: {list(cdf.columns)}")

                for _, r in cdf.iterrows():
                    email_key = get_first_valid(r, c_email_cols).lower()
                    
                    if c_fname_cols and c_lname_cols:
                        fn = get_first_valid(r, c_fname_cols)
                        ln = get_first_valid(r, c_lname_cols)
                        name_key = normalize_name(f"{fn} {ln}")
                    else:
                        name_key = normalize_name(get_first_valid(r, c_name_cols))
                    
                    c_data = {
                        'school': get_first_valid(r, c_school_cols),
                        'school_type': get_first_valid(r, c_school_type_cols),
                        'device': get_first_valid(r, c_device_cols),
                        'event': get_first_valid(r, c_event_cols)
                    }
                    if email_key:
                        calendly_lookup[email_key] = c_data
                    if name_key:
                        calendly_lookup[name_key] = c_data
            except Exception as e:
                log_callback(f"Warning: Could not process CSV: {e}")
                
    # If the session sheet is basically empty (e.g. a blank template), use the CSVs as the primary data!
    if session_df.empty and calendly_dfs:
        log_callback("Session sheet is empty. Using provided CSVs as primary data source.")
        session_df = pd.concat(calendly_dfs, ignore_index=True)

    # Fetch master list of schools from historical data
    master_schools = []
    historical_devices_allocated = set()
    try:
        master_schools, historical_devices_allocated = client.get_historical_schools_data()
        log_callback(f"Fetched {len(master_schools)} known schools and {len(historical_devices_allocated)} historically allocated schools.")
    except Exception as e:
        log_callback(f"Warning: Could not fetch historical schools: {e}")

    # Detect columns dynamically
    fname_cols = find_cols(session_df.columns, ['first name', 'given name'])
    lname_cols = find_cols(session_df.columns, ['last name', 'surname', 'family name'])
    name_cols = find_cols(session_df.columns, ['student name', 'learner name', 'invitee name', 'full name', 'name', 'student'], ['name', 'student'])
    
    school_cols = find_cols(session_df.columns, ['school', 'school name', 'organisation', 'organization', 'organisation name', 'organization name', 'employer'], ['school', 'organisation'], exclude_words=['type', 'best describes', 'postcode', 'device', 'required', 'address'])
    email_cols = find_cols(session_df.columns, ['email', 'email address', 'invitee email'], ['email'])
    event_cols = find_cols(session_df.columns, ['session name', 'class name', 'event name', 'event type name', 'course name', 'course'], ['event type', 'session', 'event'])
    date_cols = find_cols(session_df.columns, ['start date', 'event date', 'date', 'session date'], ['date'])
    device_cols = find_cols(session_df.columns, ['device', 'device pack'], ['anapen', 'epipen', 'neffy', 'jext', 'trainer device', 'training equipment', 'device pack', 'devices for your assessment', 'which device'], exclude_words=['dietary', 'wheelchair', 'accommodations'])
    
    school_type_cols = find_cols(session_df.columns, ['school type', 'organisation type', 'organization type', 'which best describes your school?'], ['best describes your school', 'organisation type'])
    attendance_cols = find_cols(session_df.columns, ['attended', 'attendance'], ['attend'])
    status_cols = find_cols(session_df.columns, ['status', 'enrolment status', 'booking status', 'attendance status'], [])
    
    active_rows = []
    processed_schools_this_run = set()
    
    unique_schools = set()
    school_type_counts = {'Independent': 0, 'Government': 0, 'Catholic': 0, 'Other': 0, 'Unknown': 0}
    
    # Advanced Metrics for Summary Data Tab
    adv_schools_trained = {'Government': set(), 'Catholic': set(), 'Independent': set()}
    adv_staff_trained = {'Government': 0, 'Catholic': 0, 'Independent': 0}
    adv_course_breakdown = {}

    log_callback("Applying absentee checks, fuzzy matching, and single-device allocation logic...")
    
    # We build a dynamic master list from the historical data for progressive fuzzy matching
    known_schools = list(master_schools)
    
    if True:
        for _, row in session_df.iterrows():
            # Name Extraction
            first_name = ""
            last_name = ""
            full_name = ""
            if fname_cols and lname_cols:
                first_name = get_first_valid(row, fname_cols)
                last_name = get_first_valid(row, lname_cols)
                full_name = f"{first_name} {last_name}".strip()
            elif name_cols:
                full_name = get_first_valid(row, name_cols)
                parts = full_name.split(' ', 1)
                first_name = parts[0] if parts else ''
                last_name = parts[1] if len(parts) > 1 else ''
            else:
                # Fallback for Axcelerate attendance summary which might have name in Column D (Index 3)
                if len(session_df.columns) > 3:
                    full_name = str(row.iloc[3]).strip()
                    parts = full_name.split(' ', 1)
                    first_name = parts[0] if parts else ''
                    last_name = parts[1] if len(parts) > 1 else ''
    
            email = get_first_valid(row, email_cols).lower()
            norm_name = normalize_name(full_name)
            c_match = calendly_lookup.get(email) or calendly_lookup.get(norm_name)
            
            if not c_match and norm_name:
                for k, v in calendly_lookup.items():
                    if '@' not in k and len(k) > 4:
                        # 1. Subset Check
                        k_parts = set(k.split())
                        n_parts = set(norm_name.split())
                        if k_parts.issubset(n_parts) or n_parts.issubset(k_parts):
                            c_match = v
                            break
                            
                        # 2. General Fuzzy Match (Catches missing spaces like "mc cullough" vs "mccullough")
                        if fuzz.token_sort_ratio(k, norm_name) > 85:
                            c_match = v
                            break
                            
                        # 3. Last Name + First Name Prefix/Initial Match
                        k_list = k.split()
                        n_list = norm_name.split()
                        if len(k_list) >= 2 and len(n_list) >= 2:
                            last_name_k = "".join(k_list[1:])
                            last_name_n = "".join(n_list[1:])
                            
                            if fuzz.ratio(last_name_k, last_name_n) > 90 or fuzz.token_set_ratio(last_name_k, last_name_n) > 90:
                                # Strong first name match (prefix or initial)
                                if k_list[0][:4] == n_list[0][:4] and len(k_list[0]) >= 3:
                                    c_match = v
                                    break
                                if len(k_list[0]) == 1 and k_list[0] == n_list[0][0]:
                                    c_match = v
                                    break
                                if len(n_list[0]) == 1 and n_list[0] == k_list[0][0]:
                                    c_match = v
                                    break
                                    
                                # Unique Last Name Fallback
                                possible_matches = 0
                                for c_key in calendly_lookup.keys():
                                    if '@' not in c_key:
                                        c_k_list = c_key.split()
                                        if len(c_k_list) >= 2:
                                            c_last_name = "".join(c_k_list[1:])
                                            if fuzz.ratio(c_last_name, last_name_n) > 90 or fuzz.token_set_ratio(c_last_name, last_name_n) > 90:
                                                possible_matches += 1
                                                
                                if possible_matches == 1 and k_list[0][0] == n_list[0][0]:
                                    c_match = v
                                    break
                                    
                # 4. Email Prefix Fallback (e.g. Shirley Weir with email sweir@...)
                if not c_match and norm_name:
                    n_list = norm_name.split()
                    if len(n_list) >= 2:
                        last_name_n = "".join(n_list[1:])
                        first_initial = n_list[0][0]
                        for k, v in calendly_lookup.items():
                            if '@' in k:
                                prefix = k.split('@')[0].replace(".", "").replace("-", "").replace("_", "")
                                # If last name is in email prefix, AND first initial is in email prefix
                                if last_name_n in prefix and first_initial in prefix:
                                    # Ensure it's somewhat unique or just take the first match
                                    c_match = v
                                    break
                                # Also check if first name + last name is in prefix
                                if "".join(n_list) in prefix:
                                    c_match = v
                                    break
                            
            c_match = c_match or {}
            
            # Absent Check
            is_absent = False
            att_val = get_first_valid(row, attendance_cols).lower()
            stat_val = get_first_valid(row, status_cols).lower()
            
            if att_val in ['yes', 'true', 'attended']:
                is_absent = False
            else:
                if att_val in ['no', 'false', 'absent']:
                    is_absent = True
                if stat_val in ['absent', 'no show', 'did not attend', 'cancelled', 'withdrawn']:
                    is_absent = True
                    
            if is_absent:
                log_callback(f"Skipping {full_name} - marked as absent/cancelled.")
                continue
            
            # Prioritize Calendly CSV data over the Session Sheet for demographics
            raw_school = c_match.get('school', '')
            if not raw_school:
                raw_school = get_first_valid(row, school_cols)
                
            event_name = c_match.get('event', '')
            if not event_name:
                event_name = get_first_valid(row, event_cols)
                
            device_resp = c_match.get('device', '')
            if not device_resp:
                device_resp = get_first_valid(row, device_cols)
    
            raw_school_type = c_match.get('school_type', '')
            if not raw_school_type:
                raw_school_type = get_first_valid(row, school_type_cols)
    
            if not raw_school:
                raw_school = "Unknown School"
                log_callback(f"Warning: {full_name} has no school found. Defaulting to 'Unknown School'.")
                
            # Progressive Fuzzy match to normalize
            if known_schools:
                import re
                def get_core_school(s):
                    cs = str(s).lower().replace("'", "").replace(".", "").replace(",", "").replace("-", " ")
                    stopwords = r'\b(school|schools|primary|secondary|college|grammar|grammer|p 12|p 9|p 8|high|early|learning|centre|center|catholic|anglican|independent|government|state|academy|institute)\b'
                    cs = re.sub(stopwords, ' ', cs)
                    return re.sub(r'\s+', ' ', cs).strip()
                    
                best_match = None
                best_score = 0
                raw_core = get_core_school(raw_school)
                
                for ks in known_schools:
                    ks_core = get_core_school(ks)
                    
                    # 1. Exact core match
                    if raw_core == ks_core and raw_core != "":
                        score = 100
                    else:
                        # 2. Fuzzy sort ratio on core words
                        score = fuzz.token_sort_ratio(raw_core, ks_core)
                        
                    if score > best_score:
                        best_score = score
                        best_match = ks
                        
                if best_match and best_score > 85:
                    normalized_school = best_match
                else:
                    normalized_school = raw_school
                    known_schools.append(normalized_school)
            else:
                normalized_school = raw_school
                known_schools.append(normalized_school)
            
            # Date fallback
            date_val = get_first_valid(row, date_cols).split(' ')[0]
            if not date_val:
                import re
                date_str = target_tab_name
                if re.match(r'^\d{8}$', date_str):
                    date_val = f"{date_str[:2]}/{date_str[2:4]}/{date_str[4:]}"
                else:
                    date_val = target_tab_name
                    
            formatted_date_string = date_val
            base_formatted_date = date_val
            try:
                import datetime
                dt = datetime.datetime.strptime(date_val, "%d/%m/%Y")
                
                def ordinal(n):
                    if 11 <= (n % 100) <= 13: return str(n) + 'th'
                    return str(n) + {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
                    
                formatted_date_string = f"{ordinal(dt.day)} {dt.strftime('%B %Y')}"
                base_formatted_date = formatted_date_string
            except:
                pass
                
            # Date format suffix for jext/neffy
            if workshop_type == 'jext_neffy':
                formatted_date_string += " Workshop"
            
            # School Type
            school_type = 'Unknown'
            if raw_school_type:
                rs_lower = raw_school_type.lower()
                if 'independent' in rs_lower:
                    school_type = 'Independent'
                elif 'government' in rs_lower:
                    school_type = 'Government'
                elif 'catholic' in rs_lower:
                    school_type = 'Catholic'
            
            # Course Duplication Logic based on Event Name
            courses_to_add = []
            ev_lower = event_name.lower()
            
            if workshop_type == 'jext_neffy':
                courses_to_add.append(('Jext & Neffy Workshop', ''))
            else:
                if 'verifier' in ev_lower and 'anaphylaxis' in ev_lower:
                    courses_to_add.append(('Verifying the Correct Use of Adrenaline Injector Devices', '22579VIC'))
                    courses_to_add.append(('Course in First Aid Management of Anaphylaxis', '22578VIC'))
                elif 'verifier' in ev_lower:
                    courses_to_add.append(('Verifying the Correct Use of Adrenaline Injector Devices', '22579VIC'))
                else:
                    courses_to_add.append(('Course in First Aid Management of Anaphylaxis', '22578VIC'))
                
            # Device Allocation
            device_allocated = ''
            explicit_no_device = False
            dr_lower = device_resp.lower()
            
            if '4 pack' in dr_lower or ('anapen' in dr_lower and 'epipen' in dr_lower and 'neffy' in dr_lower and 'jext' in dr_lower):
                mapped_device = '4 Pack (Ana, Epi, Jext & Neffy)'
            elif '3 pack' in dr_lower or ('anapen' in dr_lower and 'epipen' in dr_lower and 'jext' in dr_lower):
                mapped_device = '3 Pack (Ana, Epi & Jext)'
            elif '2 pack' in dr_lower or 'neffy + 1 x jext' in dr_lower or ('neffy' in dr_lower and 'jext' in dr_lower):
                mapped_device = '2 Pack (Neffy & Jext)'
            elif 'do not require' in dr_lower or 'none' in dr_lower or 'no training equipment' in dr_lower:
                mapped_device = ''
                explicit_no_device = True
            else:
                if workshop_type == 'accredited':
                    mapped_device = '4 Pack (Ana, Epi, Jext & Neffy)'
                else:
                    mapped_device = '2 Pack (Neffy & Jext)'
                explicit_no_device = False
                
            if raw_school == 'Unknown School':
                mapped_device = 'Unknown'
                explicit_no_device = False
                
            highlight = False
            if normalized_school == 'Unknown School' or fuzz.token_sort_ratio(raw_school.strip().lower(), full_name.lower()) > 80:
                highlight = True
                
            # Only allocate 1 per school, unless it's Unknown School or an Independent school
            if (normalized_school not in processed_schools_this_run or normalized_school == 'Unknown School' or school_type == 'Independent') and not explicit_no_device:
                if mapped_device:
                    device_allocated = mapped_device
                    processed_schools_this_run.add(normalized_school)
                    
            # Update metrics
            if normalized_school not in unique_schools:
                unique_schools.add(normalized_school)
            school_type_counts[school_type] += 1
            
            if not highlight:
                if school_type in adv_schools_trained:
                    adv_schools_trained[school_type].add(normalized_school)
                    adv_staff_trained[school_type] += 1
                
            for course_val, code_val in courses_to_add:
                
                if not highlight:
                    c_key = f"{code_val} - {course_val}" if code_val else course_val
                    if c_key not in adv_course_breakdown:
                        adv_course_breakdown[c_key] = {'Government': 0, 'Catholic': 0, 'Independent': 0}
                    if school_type in adv_course_breakdown[c_key]:
                        adv_course_breakdown[c_key][school_type] += 1
                        
                formatted_row = {
                    "First Name": first_name,
                    "Last Name": last_name,
                    "Code": code_val,
                    "Course": course_val,
                    "Date": date_val,
                    "School Name": normalized_school,
                    "School Type": school_type,
                    "Device Pack Sent": device_allocated,
                    "_highlight_yellow": highlight
                }
                active_rows.append(formatted_row)
                device_allocated = '' 
    
    log_callback(f"Processed {len(active_rows)} records.")
    log_callback(f"Allocated devices to {len(processed_schools_this_run)} distinct schools.")

    # 4. Push to Master Sheet
    adv_metrics = {
        'formatted_date': formatted_date_string if active_rows else target_tab_name,
        'date_table2': base_formatted_date if active_rows else target_tab_name,
        'raw_date': active_rows[0]['Date'] if active_rows else target_tab_name,
        'adv_schools_trained': {k: len(v) for k, v in adv_schools_trained.items()},
        'adv_staff_trained': adv_staff_trained,
        'adv_course_breakdown': adv_course_breakdown
    }
    client.append_to_master(active_rows, [], target_tab_name, workshop_type, adv_metrics=adv_metrics)
    
    # 5. Update Summary Dashboard
    client.update_summary_metrics(len(unique_schools), school_type_counts, workshop_type, adv_metrics=adv_metrics)

