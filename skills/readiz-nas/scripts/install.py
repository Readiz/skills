#!/usr/bin/env python3
"""Install a caller-supplied private profile and CLI wrapper without granting access."""
import argparse, json, os, stat, sys, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import nas as nas_cli

def ensure_private_dir(path,home):
    path=Path(path).expanduser()
    # Validate every existing component before making any change.
    cur=Path(path.anchor)
    for part in path.parts[1:]:
        cur=cur/part
        if cur.is_symlink(): raise RuntimeError('refusing symlink: '+str(cur))
        if cur.exists():
            st=cur.stat()
            if (cur==home or home in cur.parents) and st.st_uid!=os.getuid(): raise RuntimeError('refusing non-owner directory: '+str(cur))
        else:
            cur.mkdir(mode=0o700)
    st=path.stat(); old_mode=stat.S_IMODE(st.st_mode)
    if old_mode&0o022: raise RuntimeError('refusing group/other-writable private directory: '+str(path))
    if old_mode&0o077: os.chmod(path,0o700)
    return path,old_mode

def ensure_bin_dir(path,home):
    path=Path(path).expanduser(); cur=Path(path.anchor)
    for part in path.parts[1:]:
        cur=cur/part
        if cur.is_symlink(): raise RuntimeError('refusing symlink: '+str(cur))
        if not cur.exists(): cur.mkdir(mode=0o700)
        st=cur.stat()
        if (cur==home or home in cur.parents) and st.st_uid!=os.getuid(): raise RuntimeError('refusing non-owner directory: '+str(cur))
        if (cur==home or home in cur.parents) and stat.S_IMODE(st.st_mode)&0o022: raise RuntimeError('refusing group/other-writable directory: '+str(cur))
    return path

def put(path,data,mode):
    path=Path(path)
    if path.is_symlink(): raise RuntimeError('refusing symlink file: '+str(path))
    if path.exists():
        st=path.stat()
        if st.st_uid!=os.getuid(): raise RuntimeError('refusing non-owner file: '+str(path))
        if path.read_bytes()==data:
            if stat.S_IMODE(st.st_mode)!=mode: os.chmod(path,mode)
            return 'unchanged'
        raise RuntimeError('refusing to overwrite existing file: '+str(path))
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=str(path.parent))
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.link(tmp,path)
        return 'created'
    finally:
        try: os.unlink(tmp)
        except OSError: pass

def shquote(s): return "'"+s.replace("'","'\\''")+"'"

def install(profile,skill_dir,home=None):
    home=Path(home or Path.home()).expanduser().resolve()
    prof=Path(profile).expanduser()
    if prof.is_symlink(): raise RuntimeError('refusing profile symlink')
    pst=prof.stat()
    if pst.st_uid!=os.getuid() or pst.st_mode&0o077: raise RuntimeError('profile must be owner-only readable')
    data=json.loads(prof.read_text())
    if not isinstance(data,dict) or not all(isinstance(data.get(k),str) for k in ('mount','source')): raise RuntimeError('profile requires mount and source strings')
    launcher=data.get('launcher',str(home/'.local/bin/readiz-nas-python'))
    if not os.path.isabs(launcher) or not os.path.isfile(launcher) or not os.access(launcher,os.X_OK): raise RuntimeError('fixed launcher unavailable')
    data['launcher']=launcher
    cfgdir,cfg_old_mode=ensure_private_dir(home/'.config/readiz-nas',home)
    statedir,_=ensure_private_dir(home/'.local/state/readiz-nas',home)
    bindir=ensure_bin_dir(home/'.local/bin',home)
    data.setdefault('state_dir',str(statedir))
    nas_cli.validate_config(data)
    config=cfgdir/'access.json'
    put(config,(json.dumps(data,indent=2)+'\n').encode(),0o600)
    script=(Path(skill_dir).resolve()/'scripts/nas.py')
    wrapper=bindir/'readiz-nas'
    body=('#!/bin/sh\nexec '+shquote(launcher)+' '+shquote(str(script))+' "$@"\n').encode()
    put(wrapper,body,0o700)
    receipt=statedir/'install-receipt.json'
    receipt_data={'version':1,'config_dir':str(cfgdir),'config_dir_mode_before':oct(cfg_old_mode),'config_dir_mode_after':oct(stat.S_IMODE(cfgdir.stat().st_mode)),'created_files':[str(config),str(wrapper)],'macos_permission_granted':False}
    if receipt.exists():
        if receipt.is_symlink() or receipt.stat().st_uid!=os.getuid(): raise RuntimeError('unsafe install receipt')
        old=json.loads(receipt.read_text())
        if old.get('created_files')!=receipt_data['created_files']: raise RuntimeError('install receipt conflict')
    else: put(receipt,(json.dumps(receipt_data,indent=2)+'\n').encode(),0o600)
    return {'status':'installed','profile':str(config),'wrapper':str(wrapper),'macos_permission_granted':False}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--profile',required=True); p.add_argument('--skill-dir',required=True); a=p.parse_args()
    try: print(json.dumps(install(a.profile,a.skill_dir)))
    except Exception as e: raise SystemExit(str(e))
if __name__=='__main__': main()
