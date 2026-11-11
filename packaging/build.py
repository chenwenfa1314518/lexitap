"""Build the installable package for the system this runs on (PyInstaller can't build for other
systems: .github/workflows/build.yml builds all of them on GitHub). The packages land in dist/:

  Windows  lexitap-<version>-windows-x64-setup.exe  installer (if Inno Setup is installed): Start menu
                                                       entry, listed under "Open with" for PDFs
           lexitap-<version>-windows-x64.zip        portable: unzip anywhere and run the .exe
  macOS    lexitap-<version>-macos-<arch>.dmg       drag the app to Applications; opens PDFs from Finder
  Linux    lexitap_<version>_<arch>.deb             sudo apt install ./lexitap_<version>_<arch>.deb
           lexitap-<version>-linux-<arch>.tar.xz    unpack anywhere and run lexitap/lexitap

Needs: pip install "pymupdf<2" pyinstaller pillow    Run: python packaging/build.py
(PyMuPDF 1.x: version 2 works as well, but makes every package more than twice as large.)
"""
import os, platform, plistlib, re, shutil, string, subprocess, sys, tarfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST, BUILD, ICON = os.path.join(ROOT, 'dist'), os.path.join(ROOT, 'build'), os.path.join(HERE, 'icon.png')
ICO = os.path.join(BUILD, 'icon.ico')  # the icon for Windows, in the sizes that Windows apps carry
ICO_SIZES = 16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 256
with open(os.path.join(ROOT, 'lexitap.py'), encoding='utf-8') as f:
    VERSION = re.search(r"__version__ = '(.+?)'", f.read()).group(1)
NAME, ID = 'LexiTap', 'lexitap'
ARCH = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64'}.get(platform.machine().lower(), platform.machine().lower())
OUT = os.path.join(DIST, f'{ID}-{VERSION}-')

ISS = r'''[Setup]
AppId=$ID
AppName=$NAME
AppVersion=$VERSION
VersionInfoVersion=$VERSION
DefaultDirName={autopf}\$NAME
DisableProgramGroupPage=yes
; For the current user only: no administrator rights, so Windows shows no "unknown publisher" prompt.
; (Setup /ALLUSERS installs for everyone, and then asks for those rights.)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Setup and the uninstaller ask to close a running copy first (the app holds this mutex while it runs):
; its files can't be replaced under it, and a half-replaced app would not start any more.
AppMutex=$ID
CloseApplications=no
ChangesAssociations=yes
SetupIconFile=$ICO
UninstallDisplayIcon={app}\$EXE
UninstallDisplayName=$NAME
OutputDir=$DIST
OutputBaseFilename=$OUT
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: desktopicon; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "$SRC\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\$NAME"; Filename: "{app}\$EXE"
Name: "{autodesktop}\$NAME"; Filename: "{app}\$EXE"; Tasks: desktopicon

[Registry]
; A file type of the app's own, offered for .pdf under "Open with", and the app's entry for Settings > Default
; apps. Only the user can make an app the default: Windows asks at the next PDF opened, or "Open with" > Always.
Root: HKA; Subkey: "Software\Classes\$ID.pdf"; ValueType: string; ValueData: "PDF"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\$ID.pdf\DefaultIcon"; ValueType: string; ValueData: "{app}\$EXE,0"
Root: HKA; Subkey: "Software\Classes\$ID.pdf\shell\open\command"; ValueType: string; ValueData: """{app}\$EXE"" ""%1"""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "$ID.pdf"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\$ID"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\$ID\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "$NAME"
Root: HKA; Subkey: "Software\$ID\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "PDF viewer that translates the selected sentences"
Root: HKA; Subkey: "Software\$ID\Capabilities\FileAssociations"; ValueType: string; ValueName: ".pdf"; ValueData: "$ID.pdf"
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "$ID"; ValueData: "Software\$ID\Capabilities"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\$EXE"; Description: "{cm:LaunchProgram,$NAME}"; Flags: nowait postinstall skipifsilent
'''

# what Windows shows for the .exe: its name under "Open with" and in Task Manager, its version in Properties
VERSION_INFO = '''VSVersionInfo(ffi=FixedFileInfo(filevers=($TUPLE), prodvers=($TUPLE)), kids=[
    StringFileInfo([StringTable('040904B0', [StringStruct('FileDescription', '$NAME'), StringStruct('ProductName', '$NAME'),
                                             StringStruct('FileVersion', '$VERSION'), StringStruct('ProductVersion', '$VERSION')])]),
    VarFileInfo([VarStruct('Translation', [0x409, 1200])])])
'''

CONTROL = '''Package: lexitap
Version: $VERSION
Architecture: $ARCH
Maintainer: lexitap <noreply@example.com>
Section: text
Priority: optional
Description: PDF viewer that translates the selected sentences into Chinese
 Select text across lines and pages; whole sentences are translated by a large
 language model (DeepSeek or any OpenAI-compatible API).
'''

DESKTOP = '''[Desktop Entry]
Type=Application
Name=$NAME
Comment=Select text across lines and pages, translate whole sentences into Chinese
Exec=lexitap %F
Icon=lexitap
Terminal=false
Categories=Office;Viewer;
MimeType=application/pdf;
'''


def pyinstaller(name):
    """Bundle the app with its own Python into build/app/<name> (macOS: also build/app/<name>.app)."""
    import PyInstaller.__main__
    args = [os.path.join(ROOT, 'lexitap.py'), '--name', name, '--windowed', '--noconfirm', '--clean',
            '--osx-bundle-identifier', 'local.lexitap',
            '--distpath', os.path.join(BUILD, 'app'), '--workpath', BUILD, '--specpath', BUILD]
    for m in ('PIL', 'numpy', 'pandas', 'fontTools'):  # optional extras of PyMuPDF that the app doesn't use
        args += ['--exclude-module', m]
    if sys.platform == 'win32':  # icon, name and version go into the .exe itself: the app ships no icon file
        from PIL import Image
        os.makedirs(BUILD, exist_ok=True)
        Image.open(ICON).save(ICO, sizes=[(s, s) for s in ICO_SIZES])
        with open(os.path.join(BUILD, 'version.txt'), 'w', encoding='utf-8') as f:
            f.write(string.Template(VERSION_INFO).substitute(NAME=NAME, VERSION=VERSION,
                                                             TUPLE=', '.join((VERSION.split('.') + ['0'] * 3)[:4])))
        args += ['--icon', ICO, '--version-file', f.name]
    else:  # macOS takes the app's icon from its bundle; a Linux window needs the picture as a file
        args += ['--icon', ICON] + ['--add-data', ICON + os.pathsep + '.'] * (sys.platform != 'darwin')
    PyInstaller.__main__.run(args)
    return os.path.join(BUILD, 'app', name)


def sign(path):
    """Sign a file, if the environment variable LEXITAP_SIGN holds the command for it ($f: the file),
    for example:  signtool sign /a /fd sha256 /tr http://timestamp.digicert.com /td sha256 $f"""
    if os.environ.get('LEXITAP_SIGN'):
        subprocess.run(os.environ['LEXITAP_SIGN'].replace('$f', f'"{path}"'), shell=True, check=True)


def inno_setup():
    """Inno Setup's compiler, where Windows has Inno Setup registered as installed."""
    import winreg
    for root, view in [(winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY)]:
        try:
            with winreg.OpenKey(root, r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1', 0,
                                winreg.KEY_READ | view) as key:
                return os.path.join(winreg.QueryValueEx(key, 'InstallLocation')[0], 'ISCC.exe')
        except OSError:
            pass


def windows():
    app = pyinstaller(NAME)
    inner = os.path.join(app, '_internal')  # leave out what the app never uses: copies of the C runtime that
    for name in os.listdir(inner):          # Windows 10 and 11 have themselves, Tcl's time zones and month names
        if name.startswith('api-ms-win-') or name == 'ucrtbase.dll':
            os.remove(os.path.join(inner, name))
    for name in 'tzdata', 'msgs':
        shutil.rmtree(os.path.join(inner, '_tcl_data', name), ignore_errors=True)
    sign(os.path.join(app, NAME + '.exe'))
    shutil.make_archive(OUT + f'windows-{ARCH}', 'zip', os.path.dirname(app), NAME)
    iscc = shutil.which('iscc') or inno_setup()
    if not iscc:
        print('Inno Setup (https://jrsoftware.org/isinfo.php) is not installed: made the zip, no installer.')
        return
    iss, out = os.path.join(BUILD, 'setup.iss'), os.path.basename(OUT) + f'windows-{ARCH}-setup'
    with open(iss, 'w', encoding='utf-8-sig') as f:  # Inno Setup reads UTF-8 when the file starts with a BOM
        f.write(string.Template(ISS).substitute(ID=ID, NAME=NAME, VERSION=VERSION, EXE=NAME + '.exe', ICO=ICO,
                                                SRC=app, DIST=DIST, OUT=out))
    subprocess.run([iscc, '/Q', iss], check=True)
    sign(os.path.join(DIST, out + '.exe'))


def macos():
    app = pyinstaller(NAME) + '.app'
    plist = os.path.join(app, 'Contents', 'Info.plist')
    with open(plist, 'rb') as f:
        info = plistlib.load(f)
    info.update(CFBundleShortVersionString=VERSION, CFBundleVersion=VERSION, NSHighResolutionCapable=True,
                CFBundleDocumentTypes=[{'CFBundleTypeName': 'PDF', 'CFBundleTypeRole': 'Viewer',  # "Open With"
                                        'LSHandlerRank': 'Alternate', 'LSItemContentTypes': ['com.adobe.pdf']}])
    with open(plist, 'wb') as f:
        plistlib.dump(info, f)
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', app], check=True)  # seal it again after the edit
    stage = os.path.join(BUILD, 'dmg')
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    shutil.move(app, stage)
    os.symlink('/Applications', os.path.join(stage, 'Applications'))  # the disk image says: drag the app here
    for _ in range(3):  # hdiutil is sometimes "busy" on build servers
        if subprocess.run(['hdiutil', 'create', '-volname', NAME, '-srcfolder', stage, '-ov', '-format', 'ULMO',
                           OUT + f'macos-{ARCH}.dmg']).returncode == 0:  # ULMO: the smallest; macOS 10.15 and later
            return
        time.sleep(10)
    sys.exit('hdiutil failed')


def linux():
    app = pyinstaller(ID)
    with tarfile.open(OUT + f'linux-{ARCH}.tar.xz', 'w:xz') as tar:
        tar.add(app, ID)
    if not shutil.which('dpkg-deb'):
        print('dpkg-deb not found: made the .tar.xz, no .deb.')
        return
    root, arch = os.path.join(BUILD, 'deb'), subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(app, os.path.join(root, 'opt', ID), symlinks=True)
    for path, text in [('DEBIAN/control', CONTROL), (f'usr/share/applications/{ID}.desktop', DESKTOP)]:
        os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
        with open(os.path.join(root, path), 'w', encoding='utf-8') as f:
            f.write(string.Template(text).substitute(VERSION=VERSION, ARCH=arch, NAME=NAME))
    os.makedirs(os.path.join(root, 'usr/share/icons/hicolor/512x512/apps'))
    shutil.copy(ICON, os.path.join(root, f'usr/share/icons/hicolor/512x512/apps/{ID}.png'))
    os.makedirs(os.path.join(root, 'usr/bin'))
    os.symlink(f'/opt/{ID}/{ID}', os.path.join(root, 'usr/bin', ID))  # the "lexitap" command
    subprocess.run(['dpkg-deb', '-Zxz', '--build', '--root-owner-group', root,  # xz: small, and every Debian reads it
                    os.path.join(DIST, f'{ID}_{VERSION}_{arch}.deb')], check=True)


if __name__ == '__main__':
    shutil.rmtree(DIST, ignore_errors=True)
    os.makedirs(DIST)
    {'win32': windows, 'darwin': macos}.get(sys.platform, linux)()
    print('\nBuilt:', *sorted(os.listdir(DIST)), sep='\n  ')
