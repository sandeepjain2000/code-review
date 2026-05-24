import os

file_path = 'frontend/index.html'
terms = ['Files Completed', 'Files In-Process', 'Round 2', 'Batch Info', 'Live Event Log', 'tab']

if os.path.exists(file_path):
    print(f"Reading {file_path}...")
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    print(f"Total lines: {len(lines)}")
    for i, line in enumerate(lines):
        line_strip = line.strip()
        # Find exact matches for tab names
        for term in ['Files Completed', 'Files In-Process', 'Round 2', 'Batch Info', 'Live Event Log']:
            if term in line_strip:
                print(f"MATCH: Line {i+1}: {line_strip}")
else:
    print(f"File not found: {file_path}")
