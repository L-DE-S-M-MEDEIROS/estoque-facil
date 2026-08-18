$ErrorActionPreference = "Stop"
$python = "C:\Users\Larissi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $python -m PyInstaller --noconfirm --clean --windowed --onefile --name "Estoque Facil" --icon "src\app\favicon.ico" --collect-all PIL app.py
