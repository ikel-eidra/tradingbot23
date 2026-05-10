# PyInstaller spec for TradingBot23
# Build with: pyinstaller tradingbot23.spec

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('.env.example', '.'),
    ],
    hiddenimports=[
        'bot',
        'bot.config',
        'bot.main',
        'bot.setup_wizard',
        'bot.modules',
        'bot.modules.data_fetcher',
        'bot.modules.futures_trader',
        'bot.modules.strategy',
        'bot.modules.backtester',
        'binance',
        'binance.client',
        'binance.exceptions',
        'dotenv',
        'requests',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TradingBot23',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    icon=None,
)
