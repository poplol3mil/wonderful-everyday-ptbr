import argparse, base64, hashlib, json, struct, sys, os
from pathlib import Path

APP = "Wonderful Everyday PT-BR - Capitulos 1 e 2"
STATE = ".wonderful-everyday-ptbr"

def sha(data): return hashlib.sha256(data).hexdigest()

def decode_dsc(data):
    if not data.startswith(b"DSC FORMAT 1.00\0"): return data
    key,size,count=struct.unpack_from("<III",data,16); magic=struct.unpack_from("<H",data)[0]<<16; codes=[]
    for symbol,value in enumerate(data[32:544]):
        v0=20021*(key&0xffff); v1=((magic|(key>>16))*20021+key*346+(v0>>16))&0xffff
        key=((v1<<16)+(v0&0xffff)+1)&0xffffffff; depth=(value-(v1&255))&255
        if depth: codes.append((depth,symbol))
    codes.sort(); table={}; code=prev=0
    for depth,symbol in codes: code<<=depth-prev; table[depth,code]=symbol; code+=1; prev=depth
    pos=544*8
    def bits(n):
        nonlocal pos
        value=0
        for _ in range(n): value=(value<<1)|((data[pos//8]>>(7-pos%8))&1); pos+=1
        return value
    out=bytearray(); maximum=max(d for d,_ in codes)
    for _ in range(count):
        value=0
        for depth in range(1,maximum+1):
            value=(value<<1)|bits(1); symbol=table.get((depth,value))
            if symbol is not None: break
        if symbol<256: out.append(symbol)
        else:
            offset=bits(12)+2; length=(symbol&255)+2
            for _ in range(length): out.append(out[-offset])
    if len(out)!=size: raise ValueError("Falha ao descompactar o roteiro original")
    return bytes(out)

def extract_member(archive, member):
    raw=archive.read_bytes()
    if raw[:12]!=b"BURIKO ARC20": raise ValueError("Arquivo ARC incompativel")
    count=struct.unpack_from("<I",raw,12)[0]; base=16+128*count
    for i in range(count):
        entry=raw[16+128*i:16+128*(i+1)]; name=entry[:96].split(b"\0",1)[0].decode("cp932")
        offset,size=struct.unpack_from("<II",entry,96)
        if name==member: return decode_dsc(raw[base+offset:base+offset+size])
    raise ValueError(f"Roteiro {member} nao encontrado em {archive.name}")

def rebuild_archive(raw, replacements, additions=None):
    if raw[:12]!=b"BURIKO ARC20": raise ValueError("Arquivo ARC incompativel")
    count=struct.unpack_from("<I",raw,12)[0]; base=16+128*count
    entries=[]; found=set(); additions=set(additions or ())
    for i in range(count):
        record=bytearray(raw[16+128*i:16+128*(i+1)])
        name=record[:96].split(b"\0",1)[0].decode("cp932")
        offset,size=struct.unpack_from("<II",record,96)
        payload=raw[base+offset:base+offset+size]
        if name in replacements: payload=replacements[name]; found.add(name)
        entries.append((record,payload))
    missing=set(replacements)-found-additions
    if missing: raise ValueError("Recurso ausente: "+sorted(missing)[0])
    for name in sorted(additions):
        if name in found or any(r[:96].split(b"\0",1)[0].decode("cp932")==name for r,_ in entries):
            raise ValueError("Recurso duplicado: "+name)
        encoded=name.encode("cp932")
        if len(encoded)>95: raise ValueError("Nome de recurso muito longo")
        record=bytearray(128); record[:len(encoded)]=encoded
        entries.append((record,replacements[name]))
    output=bytearray(b"BURIKO ARC20"+struct.pack("<I",len(entries))); offset=0
    for record,payload in entries:
        struct.pack_into("<II",record,96,offset,len(payload)); output.extend(record); offset+=len(payload)
    for _,payload in entries: output.extend(payload)
    return bytes(output)

def load_manifest():
    base=Path(sys.executable).parent if getattr(sys,"frozen",False) else Path(__file__).parent
    return json.loads((base/"patch-manifest.json").read_text(encoding="utf-8"))

def safe_name(value):
    if not isinstance(value,str) or not value or value in ('.','..') or any(c in value for c in '/\\:'):
        raise ValueError('Nome de arquivo invalido no manifesto.')
    return value

def assert_game_closed(game):
    if os.name != 'nt': return
    import ctypes
    ctypes.windll.kernel32.CreateMutexW.restype=ctypes.c_void_p
    ctypes.windll.kernel32.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=ctypes.windll.kernel32.CreateMutexW(None,False,'Local\\WonderfulEverydayPTBRInstaller')
    if ctypes.windll.kernel32.GetLastError()==183:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise ValueError('Outra instancia do instalador esta aberta.')
    ctypes.windll.kernel32.CloseHandle(handle)
    # An executable opened without write sharing cannot be opened for write while running.
    exe=game/'BGI.exe'
    if exe.exists():
        try:
            with exe.open('r+b'): pass
        except PermissionError:
            raise ValueError('Feche o jogo antes de instalar ou remover o patch.')

def write_atomic(path,data):
    temp=path.with_name(path.name+'.ptbr.tmp')
    temp.write_bytes(data)
    temp.replace(path)

def install(game):
    game=game.resolve(); assert_game_closed(game); manifest=load_manifest()
    state=game/STATE; receipt_path=state/'receipt.json'; backup=state/'backup'
    if receipt_path.exists():
        receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
        if receipt.get('version')!=manifest['version']:
            raise ValueError('Desinstale a versao anterior antes de atualizar.')
        verify(game)
        return 'Esta versao ja esta instalada e foi verificada. Backup preservado.'
    prepared=[]; prepared_archives=[]; seen=set(); archives={}
    for item in manifest['files']:
        name=safe_name(item['member']); archive_name=safe_name(item['archive'])
        if name in seen: raise ValueError('Roteiro duplicado no manifesto.')
        seen.add(name); archive=game/archive_name
        if archive_name not in archives:
            if sha(archive.read_bytes())!=item['archive_sha256']:
                raise ValueError('Edicao incompativel: '+archive_name)
            archives[archive_name]=True
        original=extract_member(archive,name)
        if sha(original)!=item['source_sha256']: raise ValueError('Fonte incompativel: '+name)
        output=bytearray(original)
        for offset,value in item['pointers']:
            if offset<0 or offset+4>len(original): raise ValueError('Ponteiro invalido.')
            struct.pack_into('<I',output,offset,value)
        output.extend(base64.b64decode(item['append_base64'],validate=True))
        if sha(output)!=item['output_sha256']: raise ValueError('Patch corrompido: '+name)
        target=game/name
        if target.is_symlink(): raise ValueError('Arquivo simbolico nao permitido: '+name)
        previous=target.read_bytes() if target.exists() else None
        if previous is not None and sha(previous)!=item['output_sha256']:
            raise ValueError('Outro patch ou roteiro solto encontrado: '+name+'. Use uma copia limpa do jogo.')
        prepared.append((name,bytes(output),previous))
    for graphic in manifest.get('graphics',[]):
        archive_name=safe_name(graphic['archive']); archive=game/archive_name
        if archive.is_symlink() or not archive.is_file(): raise ValueError('Arquivo ausente: '+archive_name)
        raw=archive.read_bytes(); current=sha(raw)
        if current==graphic['output_sha256']:
            output=raw
        elif current==graphic['source_sha256']:
            replacements={}; additions=set()
            for member in graphic['members']:
                name=safe_name(member['name'])
                payload=base64.b64decode(member['packed_base64'],validate=True)
                if sha(payload)!=member['packed_sha256']: raise ValueError('Recurso grafico corrompido: '+name)
                replacements[name]=payload
                if member.get('add'): additions.add(name)
            output=rebuild_archive(raw,replacements,additions)
            if sha(output)!=graphic['output_sha256']: raise ValueError('Patch grafico invalido: '+archive_name)
        else:
            raise ValueError('Edicao incompativel ou outro patch encontrado: '+archive_name)
        prepared_archives.append((archive_name,output,raw))
    # Validate every archive and output before any installation mutation.
    backup.mkdir(parents=True,exist_ok=True)
    receipt={'version':manifest['version'],'files':[],'archives':[]}; applied=[]
    try:
        for name,output,previous in prepared:
            if previous is not None: write_atomic(backup/name,previous)
            receipt['files'].append({'name':name,'prior_exists':previous is not None,'prior_sha256':sha(previous) if previous is not None else None,'installed_sha256':sha(output)})
        for name,output,previous in prepared_archives:
            write_atomic(backup/name,previous)
            receipt['archives'].append({'name':name,'prior_sha256':sha(previous),'installed_sha256':sha(output)})
        # Keep recovery information before the first target is changed.
        write_atomic(receipt_path,json.dumps(receipt,indent=2).encode('utf-8'))
        for name,output,previous in prepared:
            write_atomic(game/name,output); applied.append((name,previous))
        for name,output,previous in prepared_archives:
            write_atomic(game/name,output); applied.append((name,previous))
    except Exception:
        for name,previous in reversed(applied):
            if previous is None: (game/name).unlink()
            else: write_atomic(game/name,previous)
        if receipt_path.exists(): receipt_path.unlink()
        raise
    return f'Instalacao concluida: {len(prepared)} roteiros e {len(prepared_archives)} arquivos de dados.'

def verify(game):
    manifest=load_manifest(); good=0
    for item in manifest['files']:
        target=game/safe_name(item['member'])
        if not target.is_file() or sha(target.read_bytes())!=item['output_sha256']:
            raise ValueError('Verificacao falhou: '+item['member'])
        good+=1
    graphics=0
    for item in manifest.get('graphics',[]):
        target=game/safe_name(item['archive'])
        if not target.is_file() or sha(target.read_bytes())!=item['output_sha256']:
            raise ValueError('Verificacao falhou: '+item['archive'])
        graphics+=1
    return f'Verificados {good}/{len(manifest["files"])} roteiros e {graphics} arquivos de dados.'

def uninstall(game):
    game=game.resolve(); assert_game_closed(game); state=game/STATE
    receipt_path=state/'receipt.json'; receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
    prepared=[]
    for item in receipt['files']:
        name=safe_name(item['name']); target=game/name
        if target.is_symlink() or not target.is_file() or sha(target.read_bytes())!=item['installed_sha256']:
            raise ValueError('Arquivo alterado; nenhuma remocao realizada: '+name)
        prior=(state/'backup'/name).read_bytes() if item['prior_exists'] else None
        if prior is not None and sha(prior)!=item.get('prior_sha256'):
            raise ValueError('Backup corrompido: '+name)
        prepared.append((target,prior,target.read_bytes()))
    for item in receipt.get('archives',[]):
        name=safe_name(item['name']); target=game/name; saved=state/'backup'/name
        if target.is_symlink() or not target.is_file() or sha(target.read_bytes())!=item['installed_sha256']:
            raise ValueError('Arquivo alterado; nenhuma remocao realizada: '+name)
        if not saved.is_file() or sha(saved.read_bytes())!=item['prior_sha256']:
            raise ValueError('Backup corrompido: '+name)
        prepared.append((target,saved.read_bytes(),target.read_bytes()))
    applied=[]
    try:
        for target,prior,current in prepared:
            if prior is None: target.unlink()
            else: write_atomic(target,prior)
            applied.append((target,current))
    except Exception:
        for target,current in reversed(applied): write_atomic(target,current)
        raise
    receipt_path.unlink()
    return f'Patch removido: {len(prepared)} arquivos restaurados.'

def gui():
    import tkinter as tk
    from tkinter import filedialog,messagebox
    root=tk.Tk(); root.title(APP); root.geometry("620x190"); path=tk.StringVar(value=r"C:\KeroQ\素晴らしき日々15th")
    tk.Label(root,text="Pasta da Wonderful Everyday 15th Anniversary Edition:").pack(anchor="w",padx=14,pady=(14,4))
    row=tk.Frame(root); row.pack(fill="x",padx=14); tk.Entry(row,textvariable=path).pack(side="left",fill="x",expand=True)
    tk.Button(row,text="Procurar",command=lambda:path.set(filedialog.askdirectory() or path.get())).pack(side="left",padx=(8,0))
    status=tk.StringVar(value="O instalador valida a versao e cria backup antes de alterar arquivos."); tk.Label(root,textvariable=status,wraplength=590).pack(padx=14,pady=14)
    buttons=tk.Frame(root); buttons.pack()
    def run(action):
        try: status.set(action(Path(path.get()))); messagebox.showinfo(APP,status.get())
        except Exception as exc: status.set(str(exc)); messagebox.showerror(APP,str(exc))
    tk.Button(buttons,text="Instalar",width=16,command=lambda:run(install)).pack(side="left",padx=5)
    tk.Button(buttons,text="Verificar",width=16,command=lambda:run(verify)).pack(side="left",padx=5)
    tk.Button(buttons,text="Desinstalar",width=16,command=lambda:run(uninstall)).pack(side="left",padx=5)
    root.mainloop()

if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--game",type=Path); parser.add_argument("--uninstall",action="store_true"); parser.add_argument("--verify",action="store_true"); args=parser.parse_args()
    if not args.game: gui()
    else:
        try:
            result=uninstall(args.game) if args.uninstall else verify(args.game) if args.verify else install(args.game)
            if sys.stdout is not None: print(result)
        except Exception as exc:
            if sys.stderr is not None: print(str(exc),file=sys.stderr)
            raise SystemExit(1)
