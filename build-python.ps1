$ErrorActionPreference = "Stop"
$python = "C:\Users\Larissi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $python -m PyInstaller --noconfirm --clean --windowed --onefile --name "Estoque Bolsas Baby" --icon "src\app\favicon.ico" --add-data "assets\brand;assets\brand" --collect-all PIL --collect-all customtkinter app.py
