from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


MAX_EXECUTABLE_SIZE = 150 * 1024 * 1024
UPDATE_DIRECTORY_PREFIX = "EstoqueBolsasBaby-Atualizacao-"
OFFICIAL_ASSET_NAMES = {"Estoque.Bolsas.Baby.exe", "Estoque Bolsas Baby.exe"}
TRUSTED_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    title: str
    notes: str
    download_url: str
    asset_name: str
    asset_size: int
    sha256: str
    release_url: str


def version_key(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", value.strip(), re.IGNORECASE)
    if not match:
        raise UpdateError("A versão publicada no GitHub não segue o padrão esperado.")
    return tuple(int(part or 0) for part in match.groups())


def _validated_url(value: object) -> str:
    url = str(value or "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in TRUSTED_DOWNLOAD_HOSTS:
        raise UpdateError("A atualização contém um endereço de download não confiável.")
    return url


def _request_json(url: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Estoque-Bolsas-Baby-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            _validated_url(response.geturl())
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError("Ainda não existe uma atualização publicada.") from error
        if error.code == 403:
            raise UpdateError("O GitHub limitou temporariamente a consulta. Tente novamente mais tarde.") from error
        raise UpdateError(f"O GitHub respondeu com erro {error.code}.") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise UpdateError("O GitHub retornou uma resposta inválida.") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError("Não foi possível conectar ao GitHub. Verifique a internet.") from error


def check_for_update(current_version: str, repository: str) -> UpdateInfo | None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise UpdateError("O serviço de atualização não está vinculado corretamente ao GitHub.")
    release = _request_json(f"https://api.github.com/repos/{repository}/releases/latest")
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise UpdateError("O GitHub não retornou uma publicação estável válida.")

    remote_version = str(release.get("tag_name") or "").lstrip("vV")
    if version_key(remote_version) <= version_key(current_version):
        return None

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("A nova versão não possui o executável oficial.")
    asset = next((item for item in assets if isinstance(item, dict) and item.get("name") in OFFICIAL_ASSET_NAMES), None)
    if asset is None:
        raise UpdateError("A nova versão não possui o executável oficial.")
    try:
        asset_size = int(asset.get("size") or 0)
    except (TypeError, ValueError) as error:
        raise UpdateError("O tamanho do executável publicado é inválido.") from error
    if asset_size <= 0 or asset_size > MAX_EXECUTABLE_SIZE:
        raise UpdateError("O tamanho do executável publicado é inválido.")
    digest = str(asset.get("digest") or "")
    if not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
        raise UpdateError("A nova versão não possui uma assinatura SHA-256 válida.")

    return UpdateInfo(
        version=remote_version,
        title=str(release.get("name") or f"Versão {remote_version}"),
        notes=str(release.get("body") or "Atualização disponível.").strip(),
        download_url=_validated_url(asset.get("browser_download_url")),
        asset_name=str(asset["name"]),
        asset_size=asset_size,
        sha256=digest.split(":", 1)[1].lower(),
        release_url=str(release.get("html_url") or ""),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def download_update(info: UpdateInfo, progress=None) -> Path:
    update_dir = Path(tempfile.mkdtemp(prefix=UPDATE_DIRECTORY_PREFIX))
    executable = update_dir / "nova-versao.exe"
    partial = update_dir / "nova-versao.exe.part"
    request = urllib.request.Request(info.download_url, headers={"User-Agent": "Estoque-Bolsas-Baby-Updater"})
    downloaded = 0
    header = bytearray()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            _validated_url(response.geturl())
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_EXECUTABLE_SIZE:
                    raise UpdateError("O executável excede o limite de tamanho permitido.")
                if len(header) < 2:
                    header.extend(chunk[: 2 - len(header)])
                output.write(chunk)
                if progress:
                    progress(downloaded, info.asset_size)
        if downloaded != info.asset_size:
            raise UpdateError("O download ficou incompleto. Tente novamente.")
        if bytes(header) != b"MZ":
            raise UpdateError("O arquivo baixado não é um executável válido do Windows.")
        if file_sha256(partial) != info.sha256:
            raise UpdateError("A verificação SHA-256 da atualização falhou.")
        os.replace(partial, executable)
        return executable
    except UpdateError:
        shutil.rmtree(update_dir, ignore_errors=True)
        raise
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        shutil.rmtree(update_dir, ignore_errors=True)
        raise UpdateError("Não foi possível baixar a atualização.") from error


def _detached_flags() -> int:
    if os.name != "nt":
        return 0
    return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


def start_update_install(downloaded_executable: Path, expected_sha256: str) -> None:
    if not getattr(sys, "frozen", False):
        raise UpdateError("A instalação automática funciona somente no aplicativo instalado.")
    target = Path(sys.executable).resolve()
    update_dir = downloaded_executable.resolve().parent
    helper = update_dir / "EstoqueBolsasBaby-Atualizador.exe"
    shutil.copy2(target, helper)
    subprocess.Popen(
        [
            str(helper),
            "--apply-update",
            str(os.getpid()),
            str(target),
            str(downloaded_executable.resolve()),
            expected_sha256,
            str(update_dir),
        ],
        close_fds=True,
        creationflags=_detached_flags(),
    )


def _wait_for_process_exit(pid: int, timeout_seconds: int = 120) -> None:
    if os.name == "nt":
        synchronize = 0x00100000
        wait_timeout = 0x00000102
        wait_failed = 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return
        try:
            result = kernel32.WaitForSingleObject(handle, timeout_seconds * 1000)
            if result == wait_timeout:
                raise UpdateError("O aplicativo anterior demorou demais para fechar.")
            if result == wait_failed:
                raise UpdateError("O Windows não conseguiu acompanhar o fechamento do aplicativo anterior.")
        finally:
            kernel32.CloseHandle(handle)
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    raise UpdateError("O aplicativo anterior demorou demais para fechar.")


def replace_installed_executable(target: Path, downloaded: Path, expected_sha256: str) -> None:
    if not target.is_file() or not downloaded.is_file():
        raise UpdateError("Os arquivos necessários para a atualização não foram encontrados.")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256) or file_sha256(downloaded) != expected_sha256.lower():
        raise UpdateError("A verificação final SHA-256 da atualização falhou.")
    staging = target.with_name(f"{target.name}.nova")
    staging.unlink(missing_ok=True)
    try:
        shutil.copy2(downloaded, staging)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)


def refresh_windows_shell_icons() -> None:
    """Ask Explorer to discard cached executable and shortcut icons after an update."""
    if os.name != "nt":
        return
    try:
        ctypes.WinDLL("shell32", use_last_error=True).SHChangeNotify(0x08000000, 0, None, None)
    except OSError:
        pass


def _safe_update_directory(value: str) -> Path | None:
    candidate = Path(value).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if candidate.parent != temp_root or not candidate.name.startswith(UPDATE_DIRECTORY_PREFIX):
        return None
    return candidate


def run_update_helper(arguments: list[str]) -> bool:
    if len(arguments) < 7 or arguments[1] != "--apply-update":
        return False
    pid = int(arguments[2])
    target = Path(arguments[3]).resolve()
    downloaded = Path(arguments[4]).resolve()
    expected_sha256 = arguments[5]
    update_dir = _safe_update_directory(arguments[6])
    if update_dir is None or downloaded.parent != update_dir:
        raise UpdateError("A pasta temporária da atualização é inválida.")
    _wait_for_process_exit(pid)
    replace_installed_executable(target, downloaded, expected_sha256)
    refresh_windows_shell_icons()
    downloaded.unlink(missing_ok=True)
    subprocess.Popen(
        [str(target), "--cleanup-update", str(update_dir)],
        close_fds=True,
        creationflags=_detached_flags(),
    )
    return True


def schedule_update_cleanup(arguments: list[str]) -> None:
    try:
        index = arguments.index("--cleanup-update")
        update_dir = _safe_update_directory(arguments[index + 1])
    except (ValueError, IndexError):
        update_dir = None
    if update_dir is None:
        return

    def cleanup() -> None:
        for _attempt in range(12):
            time.sleep(1)
            try:
                shutil.rmtree(update_dir)
                return
            except OSError:
                continue

    threading.Thread(target=cleanup, daemon=True).start()
