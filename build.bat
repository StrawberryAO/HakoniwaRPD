@echo off
rem Build script: package the app into a standalone executable folder (dist/Hakoniwa/).
rem Optional heavy deps (chromadb / sentence-transformers / faster-whisper / torch)
rem are excluded to keep the exe small; those features degrade gracefully at runtime.
cd /d %~dp0
.venv\Scripts\pyinstaller --noconfirm --clean --windowed --onedir --name Hakoniwa ^
  --hidden-import PySide6.QtMultimedia ^
  --exclude-module torch ^
  --exclude-module transformers ^
  --exclude-module chromadb ^
  --exclude-module sentence_transformers ^
  --exclude-module faster_whisper ^
  --exclude-module ctranslate2 ^
  --exclude-module onnxruntime ^
  --exclude-module huggingface_hub ^
  --exclude-module tokenizers ^
  --exclude-module safetensors ^
  --exclude-module scipy ^
  --exclude-module sklearn ^
  --exclude-module numpy ^
  --exclude-module pandas ^
  --exclude-module sympy ^
  --exclude-module networkx ^
  main.py

echo.
echo Done. The app is in dist\Hakoniwa\Hakoniwa.exe
pause
