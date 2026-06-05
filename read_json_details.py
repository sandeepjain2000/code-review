import json

file_path = 'backend/data/reports/final/codesentinel_f2082325.json'

try:
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print("Report ID:", data.get("review_id"))
    print("Overall Status:", data.get("status"))
    print("Overall Score:", data.get("overall_score"))
    print("Total Files in Report:", len(data.get("files", [])))
    
    # Analyze files
    files = data.get("files", [])
    r1_count = 0
    r2_count = 0
    for idx, f in enumerate(files):
        is_r2 = f.get("is_round2", False)
        if is_r2:
            r2_count += 1
            print(f"File {idx+1}: {f['filename']} is R2 (time_taken: {f.get('time_taken')})")
        else:
            r1_count += 1
            
    print(f"R1 Files: {r1_count}, R2 Files: {r2_count}")
    
except Exception as e:
    print("Error:", e)
