import re
import urllib.request
import subprocess
import os

html_path = r"C:\Users\sandeep\Downloads\Claudes\code-review-tool\frontend\index.html"
babel_url = "https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.23.2/babel.min.js"

# 1. Read index.html
with open(html_path, "r", encoding="utf-8") as f:
    html = f.read()

# 2. Extract script block
match = re.search(r'<script type="text/babel"[^>]*>(.*?)</script>', html, re.DOTALL)
if not match:
    print("Error: Could not find type=text/babel script block in index.html")
    exit(1)

js_code = match.group(1).strip()
print(f"Extracted JS code length: {len(js_code)} bytes")

# 3. Download babel-standalone if not present locally
babel_local = "babel.min.js"
if not os.path.exists(babel_local):
    print("Downloading babel-standalone from CDN...")
    urllib.request.urlretrieve(babel_url, babel_local)
    print("Downloaded babel.min.js")

# 4. Write a temp node script to run the compilation
temp_node_script = "temp_compile.js"
node_code = f"""
const fs = require('fs');
const vm = require('vm');
const babelCode = fs.readFileSync('{babel_local.replace("\\\\", "/")}', 'utf8');

// Load Babel in global scope
vm.runInThisContext(babelCode);

const jsxCode = fs.readFileSync('temp_jsx.js', 'utf8');

try {{
    console.log("Compiling JSX code...");
    // Make sure Babel is defined globally
    const compiler = global.Babel || Babel;
    if (!compiler) throw new Error("Babel not loaded correctly in global context");
    
    const result = compiler.transform(jsxCode, {{
        presets: ['react']
    }});
    console.log("SUCCESS: Babel compilation completed successfully!");
}} catch (err) {{
    console.error("COMPILATION ERROR:");
    console.error(err.stack || err.message);
    process.exit(1);
}}
"""

with open("temp_jsx.js", "w", encoding="utf-8") as f:
    f.write(js_code)

with open(temp_node_script, "w", encoding="utf-8") as f:
    f.write(node_code)

try:
    res = subprocess.run(["node", temp_node_script], capture_output=True, text=True, check=True)
    print(res.stdout)
except subprocess.CalledProcessError as e:
    print(e.stdout)
    print(e.stderr)
    exit(1)
finally:
    if os.path.exists("temp_jsx.js"):
        os.remove("temp_jsx.js")
    if os.path.exists(temp_node_script):
        os.remove(temp_node_script)
