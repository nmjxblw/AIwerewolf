# search_simulator 持久化运行说明

后续使用 CLI 运行本子项目时，先加载父项目虚拟环境，再执行模块入口：

```powershell
cd C:\Codes\AIwerewolf\search_simulator
.\run_cli.ps1 --number_of_players 5 --number_of_wolves 1
```

如果本机 PowerShell 执行策略禁止运行 `.ps1`，使用不依赖执行策略的 cmd 包装脚本：

```cmd
cd /d C:\Codes\AIwerewolf\search_simulator
run_cli.cmd --number_of_players 5 --number_of_wolves 1
```

如果手动运行，请按以下顺序准备终端：

```powershell
chcp 65001
..\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = ".."
python -m search_simulator --cli
```

注意：上面的虚拟环境路径位于父项目目录 `C:\Codes\AIwerewolf\.venv`。
为了避免中文日志乱码，PowerShell / Windows Terminal 启动时建议自动执行 `chcp 65001`。
