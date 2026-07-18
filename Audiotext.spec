# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('resources/Audiotext.ico','resources')]
binaries=[]; hiddenimports=[]
for package in ('faster_whisper','ctranslate2','av','huggingface_hub','tokenizers','onnxruntime'):
    try:
        d,b,h=collect_all(package); datas+=d; binaries+=b; hiddenimports+=h
    except Exception:
        pass

a=Analysis(['main.pyw'], pathex=[], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
           hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['tkinter'], noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='Audiotext',debug=False,bootloader_ignore_signals=False,
        strip=False,upx=True,console=False,disable_windowed_traceback=False,
        icon='resources/Audiotext.ico',version='version_info.txt')
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=True,upx_exclude=[],name='Audiotext')
