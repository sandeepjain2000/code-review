CodeSentinel - AI Code Review Tool
==================================

Purpose
-------
Full-stack code review scanner utilizing an LLM backend (GPT-4o, Claude, Gemini, DeepSeek, or NVIDIA NIM) to run line-by-line analyses on source files. Exposes a Web UI (React/FastAPI) and a standalone CLI scanner.

How to Run the Backend & UI
---------------------------
1. Navigate to the backend directory:
   cd backend

2. Install dependencies (specifically note version requirements):
   pip install -r requirements.txt
   pip install "starlette<0.39.0"

   *Note: Pinning starlette<0.39.0 is required to avoid version mismatches with fastapi.*

3. Start the backend:
   python main.py

4. Open the Web UI in your browser (Note: http://localhost:8000/ui is the exact URL):
   http://localhost:8000/ui

   *Note: Do NOT double-click or open frontend/index.html via file:// because on-the-fly Babel JSX compiling requires a local server.*

How to Run the CLI Scanner
--------------------------
To scan a local directory from the command line:
1. Navigate to the project root:
   cd C:\Users\sandeep\Downloads\Claudes\code-review-tool

2. Run scanner.py against any target directory:
   python scanner.py "/path/to/project" --max-files 50

API Configuration
-----------------
1. Copy api_key.json.example to api_key.json.
2. Fill in your LLM provider API keys.
3. Treat api_key.json and the nvidia_keys folder as local-only secrets; never commit them to git.
