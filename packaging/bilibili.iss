; Inno Setup 脚本:Windows 安装包
; 构建: choco install innosetup && iscc packaging/bilibili.iss
;
; 注意:开发者姓名/邮箱不写进这里——安装包元数据同样不能带个人信息。
#define AppName "B站下载"
#define AppVersion "2.9.0"
#define AppPublisher "bilibili download"
#define AppExeName "BiliDownloader.exe"

[Setup]
AppId={{7C3A5F42-9B21-4C0E-9E7B-2A5D6C4E1B03}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\BiliDownloader
SetupIconFile=packaging\icon.ico
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=BiliDownloader-Setup-{#AppVersion}-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 未签名:SmartScreen 会拦一次,用户点「更多信息 → 仍要运行」
DisableDirPage=no
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
Source: "dist\BiliDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent
