; Inno Setup 脚本:Windows 安装包
; 构建: choco install innosetup && iscc packaging/bilibili.iss
;
; 注意:开发者姓名/邮箱不写进这里——安装包元数据同样不能带个人信息。
#define AppName "B站下载"
#define AppVersion "2.9.0"
#define AppPublisher "bilibili download"
#define AppExeName "BiliDownloader.exe"

; 仓库根的绝对路径由编译命令传入:iscc "/DMyRoot=<abs>" packaging/bilibili.iss
; 不用 {src}/{source} 这类路径常量——Inno Setup 6 里 {src} 已废弃,
; 而相对路径的基准(脚本目录)容易和编译目录混淆,踩过一次找不到路径的坑。
#ifndef MyRoot
  #error 请用 iscc "/DMyRoot=<仓库根绝对路径>" 编译
#endif

[Setup]
AppId={{7C3A5F42-9B21-4C0E-9E7B-2A5D6C4E1B03}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\BiliDownloader
SetupIconFile={#MyRoot}\packaging\icon.ico
OutputDir={#MyRoot}\dist
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=BiliDownloader-Setup-{#AppVersion}-x64
Compression=lzma2/max
; 安装包版本信息取自同一份来源(指令名是 VersionInfoVersion,
; 不是 SetupVersionInfoVersion——后者不是合法指令,Inno 会直接报错)
VersionInfoVersion={#AppVersion}
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
Source: "{#MyRoot}\dist\BiliDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent
