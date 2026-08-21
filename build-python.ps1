$ErrorActionPreference = "Stop"
$python = "C:\Users\Larissi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $python "scripts\generate_app_icons.py"
& $python -m PyInstaller --noconfirm --clean --windowed --onefile --name "Estoque Bolsas Baby" --icon "assets\brand\estoque-bolsas-baby.ico" --add-data "assets\brand;assets\brand" --collect-all PIL --collect-all customtkinter app.py
