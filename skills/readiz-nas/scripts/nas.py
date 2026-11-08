#!/usr/bin/env python3
"""Bounded NAS access through the existing fixed launcher and isolated workers."""
import argparse, base64, ctypes, hashlib, json, os, shutil, stat, subprocess, sys, tempfile, time, uuid
import fcntl
from pathlib import Path

CHUNK=1024*1024
LOCAL_MAX=512*1024*1024
FREE_FLOOR=2*1024*1024*1024

class NasError(Exception):
    def __init__(self, code, **data): self.code,self.data=code,data

def emit(obj): print(json.dumps(obj,sort_keys=True));
def fail(code, **extra): raise NasError(code,**extra)

def validate_config(x):
    allowed={'mount','source','launcher','allowed_local_roots','state_dir','min_local_free_bytes','max_local_copy_bytes','no_progress_timeout_seconds'}
    if not isinstance(x,dict) or set(x)-allowed: fail('invalid_profile',unknown_keys=sorted(set(x)-allowed) if isinstance(x,dict) else [])
    if not isinstance(x.get('mount'),str) or not Path(x['mount']).is_absolute() or not isinstance(x.get('source'),str) or not x['source']: fail('invalid_profile')
    if 'launcher' in x and (not isinstance(x['launcher'],str) or not Path(x['launcher']).is_absolute()): fail('invalid_profile')
    if 'state_dir' in x and (not isinstance(x['state_dir'],str) or not Path(x['state_dir']).is_absolute()): fail('invalid_profile')
    roots=x.get('allowed_local_roots',[])
    if not isinstance(roots,list) or any(not isinstance(r,str) or not Path(r).expanduser().is_absolute() for r in roots): fail('invalid_profile')
    for key,default,minimum,maximum in (('max_local_copy_bytes',LOCAL_MAX,1,2**63-1),('min_local_free_bytes',FREE_FLOOR,0,2**63-1),('no_progress_timeout_seconds',15,1,120)):
        val=x.get(key,default)
        if isinstance(val,bool) or not isinstance(val,(int,float)) or val<minimum or val>maximum: fail('invalid_profile_limit',setting=key)
    return x

def private_dir(path):
    path=Path(path).expanduser()
    if path.is_symlink(): fail('unsafe_state_symlink')
    if not path.exists(): path.mkdir(parents=True,mode=0o700)
    st=path.stat()
    if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)&0o077: fail('unsafe_state_permissions')
    return path

def cfg():
    p=Path.home()/'.config/readiz-nas/access.json'
    try:
        if p.is_symlink(): fail('unsafe_config_symlink')
        st=p.stat()
        if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)&0o077: fail('unsafe_config_permissions')
        return validate_config(json.loads(p.read_text()))
    except NasError: raise
    except Exception: fail('profile_unavailable')

def relaunch(c):
    launcher=c.get('launcher',str(Path.home()/'.local/bin/readiz-nas-python'))
    if not os.path.isabs(launcher) or not os.path.isfile(launcher) or not os.access(launcher,os.X_OK): fail('launcher_unavailable')
    if os.environ.get('READIZ_NAS_FIXED_LAUNCHER')!='1':
        os.execvpe(launcher,[launcher,os.path.realpath(__file__),*sys.argv[1:]],dict(os.environ,READIZ_NAS_FIXED_LAUNCHER='1'))

def preflight():
    try:
        cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        tcc=ctypes.CDLL('/System/Library/PrivateFrameworks/TCC.framework/TCC')
        cf.CFStringCreateWithCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32]; cf.CFStringCreateWithCString.restype=ctypes.c_void_p
        cf.CFRelease.argtypes=[ctypes.c_void_p]
        fn=tcc.TCCAccessPreflight; fn.argtypes=[ctypes.c_void_p,ctypes.c_void_p]; fn.restype=ctypes.c_int
        service=cf.CFStringCreateWithCString(None,b'kTCCServiceSystemPolicyNetworkVolumes',0x08000100)
        try: return fn(service,None)
        finally: cf.CFRelease(service)
    except Exception: return None

def mount_check(c, read_mount=None):
    mount,source=c.get('mount'),c.get('source')
    if not isinstance(mount,str) or not os.path.isabs(mount) or not isinstance(source,str): return False
    try:
        if read_mount is not None: output=read_mount()
        else:
            output=subprocess.check_output(['/sbin/mount'],text=True,stderr=subprocess.PIPE,timeout=5)
        lines=output.splitlines()
        for line in lines:
            fields=line.split()
            if len(fields)>=4 and fields[0]==source and fields[1]==mount and fields[2]=='smbfs' and 'rw' in fields[3].split(','): return True
            if line.startswith(source+' on '+mount+' (') and 'smbfs' in line and 'read-only' not in line and 'read only' not in line: return True
    except Exception: pass
    return False

def guard(c,auth=preflight,mounted=mount_check):
    result=auth()
    if result != 0: fail('authorization_unavailable',authorization_result=result if result in (1,2) else 'unsupported_or_unknown',nas_io=0,prompt_requested=False)
    if not mounted(c): fail('mount_mismatch',nas_io=0,prompt_requested=False)
    root=Path(c['mount'])
    if root.is_symlink(): fail('mount_symlink_refused')
    return root

def nas_path(root,raw):
    if raw.startswith('nas:'): raw=raw[4:]
    q=Path(raw); q=q if q.is_absolute() else root/q
    try: rel=q.relative_to(root)
    except ValueError: fail('path_outside_mount')
    cur=root
    for part in rel.parts:
        if part in ('..',''): fail('invalid_path')
        cur=cur/part
        if cur.is_symlink(): fail('nas_symlink_refused')
    return q

def local_path(raw,c,existing=False):
    q=Path(raw).expanduser()
    roots=[Path(p).expanduser().absolute() for p in c.get('allowed_local_roots',[])]
    if not q.is_absolute(): fail('local_path_must_be_absolute')
    if '..' in q.parts: fail('invalid_local_path')
    root=next((r for r in roots if q==r or r in q.parents),None)
    if root is None: fail('local_path_outside_allowed_roots')
    # Canonicalize configured roots so OS aliases such as /tmp are not mistaken
    # for user-created path links; reject links below the authorized root.
    base=root.resolve(); rel=q.relative_to(root); cur=base
    for part in rel.parts:
        cur=cur/part
        if cur.is_symlink(): fail('local_symlink_refused')
    q=base/rel
    return q

def identity(p):
    s=p.lstat()
    if not stat.S_ISREG(s.st_mode): fail('regular_file_required')
    return {'dev':s.st_dev,'ino':s.st_ino,'size':s.st_size,'mtime_ns':s.st_mtime_ns}

def open_nofollow(path,flags,mode=0o600):
    fd=os.open(path,flags|getattr(os,'O_NOFOLLOW',0),mode)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd); fail('regular_file_required')
    return fd

def hash_file(p,heartbeat=None):
    h=hashlib.sha256(); n=0
    with os.fdopen(open_nofollow(p,os.O_RDONLY),'rb') as f:
        while True:
            b=f.read(CHUNK)
            if not b: break
            h.update(b); n+=len(b)
            if heartbeat: heartbeat('hash',n)
    return n,h.hexdigest()

def atomic_json(p,obj):
    tmp=p.with_name(p.name+'.tmp-'+uuid.uuid4().hex)
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f: json.dump(obj,f,sort_keys=True); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,p)

def copy_worker(c,args,root,heartbeat):
    src_raw,dst_raw=args.source,args.dest
    src_nas=src_raw.startswith('nas:'); dst_nas=dst_raw.startswith('nas:')
    if src_nas==dst_nas: fail('copy_requires_one_local_and_one_nas_path')
    if src_nas:
        src=nas_path(root,src_raw[4:]); src_kind='nas'; dst=local_path(dst_raw,c)
    else:
        src=local_path(src_raw,c,existing=True); src_kind='local'; dst=nas_path(root,dst_raw[4:])
    if src==dst: fail('source_equals_destination')
    srcid=identity(src)
    local_limit=int(c.get('max_local_copy_bytes',LOCAL_MAX))
    floor=int(c.get('min_local_free_bytes',FREE_FLOOR))
    if src_kind=='nas' and srcid['size']>local_limit: fail('local_copy_size_limit',size=srcid['size'],limit=local_limit)
    if src_kind=='nas':
        dst.parent.mkdir(parents=True,exist_ok=True)
        if not any(dst==Path(r).expanduser().absolute().resolve() or Path(r).expanduser().absolute().resolve() in dst.parents for r in c.get('allowed_local_roots',[])): fail('local_path_outside_allowed_roots')
        # Verified partial and reserved destination coexist until completion.
        if shutil.disk_usage(dst.parent).free-2*srcid['size'] < floor: fail('local_free_space_floor',required_free=floor)
    heartbeat('source_fingerprint',0)
    source_bytes,source_sha=hash_file(src,heartbeat)
    if source_bytes!=srcid['size'] or identity(src)!=srcid: fail('source_changed')
    if not dst.parent.is_dir(): fail('destination_parent_missing')
    state=private_dir(Path(c.get('state_dir',str(Path.home()/'.local/state/readiz-nas'))))
    key=hashlib.sha256((str(src)+'\0'+str(dst)).encode()).hexdigest()
    receipt=state/(key+'.json'); part=dst.with_name('.'+dst.name+'.readiz-part'); reservation=dst.with_name('.'+dst.name+'.readiz-reservation')
    if receipt.exists() and not args.resume: fail('resume_required',receipt=str(receipt))
    if not receipt.exists() and args.resume: fail('receipt_missing')
    if not receipt.exists() and (dst.exists() or dst.is_symlink()): fail('destination_exists')
    if receipt.exists():
        try: rec=json.loads(receipt.read_text())
        except Exception: fail('invalid_receipt')
        if rec.get('source')!=str(src) or rec.get('destination')!=str(dst) or rec.get('source_identity')!=srcid or rec.get('source_sha256')!=source_sha: fail('source_changed_or_receipt_mismatch')
        if rec.get('status')=='complete':
            if not dst.exists() or dst.is_symlink() or identity(dst)!=rec.get('destination_identity'): fail('completed_destination_changed')
            size,sha=hash_file(dst,heartbeat)
            if size!=rec.get('bytes') or sha!=rec.get('sha256'): fail('completed_destination_changed')
            emit({'status':'complete','bytes':size,'sha256':sha,'receipt':str(receipt)}); return
        if dst.exists() or dst.is_symlink():
            recorded_destination=rec.get('destination_identity',{})
            current_destination=identity(dst)
            if current_destination['dev']!=recorded_destination.get('dev') or current_destination['ino']!=recorded_destination.get('ino') or current_destination['size']<rec.get('finalized_bytes',0): fail('destination_exists_or_changed')
        elif rec.get('destination_identity'):
            fail('reserved_destination_missing')
        if not part.exists() or part.is_symlink(): fail('partial_identity_mismatch')
        pst=part.stat(); recorded=rec.get('part_identity',{})
        if pst.st_dev!=recorded.get('dev') or pst.st_ino!=recorded.get('ino') or pst.st_size<rec.get('bytes',0): fail('partial_identity_mismatch')
        if reservation.exists():
            if reservation.is_symlink(): fail('reservation_identity_mismatch')
            rs=os.stat(reservation,follow_symlinks=False); reservation_id={'dev':rs.st_dev,'ino':rs.st_ino}
            if rec.get('reservation_identity') and rec['reservation_identity']!=reservation_id: fail('reservation_identity_mismatch')
        else:
            fd=os.open(reservation,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            os.write(fd,b'readiz-nas reservation '+key.encode()); os.fsync(fd); reservation_id={'dev':os.fstat(fd).st_dev,'ino':os.fstat(fd).st_ino}; os.close(fd)
        offset=part.stat().st_size
        if offset>srcid['size']: fail('partial_size_invalid')
        with os.fdopen(open_nofollow(src,os.O_RDONLY),'rb') as sf, os.fdopen(open_nofollow(part,os.O_RDONLY),'rb') as pf:
            sf.seek(0); remain=offset
            while remain:
                b=sf.read(min(CHUNK,remain)); z=pf.read(len(b))
                if not b or b!=z: fail('partial_prefix_mismatch')
                remain-=len(b)
                heartbeat('resume_prefix_validation',offset-remain)
        prefix_sha=hash_file(part,heartbeat)[1]
        rec.update(bytes=offset,prefix_sha256=prefix_sha,part_identity={'dev':pst.st_dev,'ino':pst.st_ino,'size':pst.st_size,'mtime_ns':pst.st_mtime_ns})
        f=os.fdopen(open_nofollow(part,os.O_WRONLY|os.O_APPEND),'ab'); rec['reservation_identity']=reservation_id
    else:
        fd=os.open(reservation,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,b'readiz-nas reservation '+key.encode()); os.fsync(fd)
        reservation_id={'dev':os.fstat(fd).st_dev,'ino':os.fstat(fd).st_ino}; os.close(fd)
        fd=os.open(part,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); pst=os.fstat(fd); f=os.fdopen(fd,'wb')
        rec={'version':1,'status':'in_progress','source':str(src),'destination':str(dst),'source_kind':src_kind,'source_identity':srcid,'source_sha256':source_sha,'part_identity':{'dev':pst.st_dev,'ino':pst.st_ino,'size':0,'mtime_ns':pst.st_mtime_ns},'reservation_identity':reservation_id,'bytes':0,'prefix_sha256':hashlib.sha256(b'').hexdigest(),'phase':'copy'}
        atomic_json(receipt,rec)
    h=hashlib.sha256(); n=part.stat().st_size
    with os.fdopen(open_nofollow(part,os.O_RDONLY),'rb') as old:
        while True:
            b=old.read(CHUNK)
            if not b: break
            h.update(b); heartbeat('partial_hash',old.tell())
    try:
        with os.fdopen(open_nofollow(src,os.O_RDONLY),'rb') as sf:
            sf.seek(n)
            while True:
                b=sf.read(CHUNK)
                if not b: break
                f.write(b); f.flush(); os.fsync(f.fileno()); h.update(b); n+=len(b)
                heartbeat('copy',n)
                st=part.stat(); rec.update(bytes=n,prefix_sha256=h.hexdigest(),part_identity={'dev':st.st_dev,'ino':st.st_ino,'size':st.st_size,'mtime_ns':st.st_mtime_ns},phase='copy'); atomic_json(receipt,rec)
        f.flush(); os.fsync(f.fileno()); f.close()
        if identity(src)!=srcid: fail('source_changed')
        rec.update(phase='source_verified'); atomic_json(receipt,rec); heartbeat('verify_source',n)
        size,sha=hash_file(part,heartbeat)
        if size!=srcid['size'] or sha!=h.hexdigest() or sha!=source_sha: fail('partial_hash_mismatch')
        rec.update(phase='ready_to_commit',bytes=size,sha256=sha,source_sha256=source_sha); atomic_json(receipt,rec)
        rst=os.stat(reservation,follow_symlinks=False)
        if (rst.st_dev,rst.st_ino)!=(reservation_id['dev'],reservation_id['ino']): fail('reservation_replaced')
        if not dst.exists():
            try: dfd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            except FileExistsError: fail('destination_exists')
            dstst=os.fstat(dfd); os.fsync(dfd); os.close(dfd)
            destination_id={'dev':dstst.st_dev,'ino':dstst.st_ino,'size':0,'mtime_ns':dstst.st_mtime_ns}
            rec.update(destination_identity=destination_id,finalized_bytes=0,phase='finalizing'); atomic_json(receipt,rec)
        else:
            destination_id=rec.get('destination_identity')
            current_destination=identity(dst)
            if current_destination['dev']!=destination_id.get('dev') or current_destination['ino']!=destination_id.get('ino'): fail('destination_exists_or_changed')
        dst_stat=identity(dst); final_offset=dst_stat['size']
        if final_offset>size: fail('destination_partial_invalid')
        with os.fdopen(open_nofollow(part,os.O_RDONLY),'rb') as pf, os.fdopen(open_nofollow(dst,os.O_RDONLY),'rb') as df:
            remain=final_offset
            while remain:
                b=pf.read(min(CHUNK,remain)); z=df.read(len(b))
                if not b or b!=z: fail('destination_prefix_mismatch')
                remain-=len(b)
                heartbeat('destination_prefix_validation',final_offset-remain)
        final_h=hashlib.sha256()
        with os.fdopen(open_nofollow(dst,os.O_RDONLY),'rb') as df:
            while True:
                b=df.read(CHUNK)
                if not b: break
                final_h.update(b); heartbeat('destination_partial_hash',df.tell())
        dest_fd=open_nofollow(dst,os.O_WRONLY|os.O_APPEND)
        opened=os.fstat(dest_fd)
        if (opened.st_dev,opened.st_ino)!=(destination_id['dev'],destination_id['ino']):
            os.close(dest_fd); fail('destination_replaced_before_write')
        with os.fdopen(dest_fd,'ab') as df, os.fdopen(open_nofollow(part,os.O_RDONLY),'rb') as pf:
            pf.seek(final_offset); written=final_offset
            while True:
                b=pf.read(CHUNK)
                if not b: break
                df.write(b); df.flush(); os.fsync(df.fileno()); final_h.update(b); written+=len(b)
                if identity(dst)['ino']!=destination_id['ino']: fail('destination_replaced_during_write')
                heartbeat('finalize_destination',written)
                st=identity(dst); rec.update(finalized_bytes=written,destination_identity={'dev':st['dev'],'ino':st['ino'],'size':st['size'],'mtime_ns':st['mtime_ns']},phase='finalizing'); atomic_json(receipt,rec)
        heartbeat('verify_destination',size)
        final_size,final_sha=hash_file(dst,heartbeat)
        if final_size!=size or final_sha!=sha or final_sha!=final_h.hexdigest(): fail('destination_verification_failed')
        destination_id=identity(dst)
        rec.update(status='complete',phase='complete',bytes=size,sha256=sha,destination_identity=destination_id); atomic_json(receipt,rec)
        partst=part.lstat()
        for owned,ident in ((reservation,reservation_id),(part,{'dev':partst.st_dev,'ino':partst.st_ino})):
            try:
                st=os.stat(owned,follow_symlinks=False)
                if (st.st_dev,st.st_ino)==(ident['dev'],ident['ino']): owned.unlink()
            except OSError: pass
        emit({'status':'complete','bytes':size,'sha256':sha,'receipt':str(receipt)})
    except BaseException as e:
        try:
            if not f.closed: f.flush(); os.fsync(f.fileno()); f.close()
            if part.exists() and not part.is_symlink():
                st=part.stat(); rec.update(status='incomplete',phase='interrupted',bytes=st.st_size,part_identity={'dev':st.st_dev,'ino':st.st_ino,'size':st.st_size,'mtime_ns':st.st_mtime_ns})
                rec['prefix_sha256']=hash_file(part,heartbeat)[1]; atomic_json(receipt,rec)
        except Exception: pass
        raise

def worker(c,command,args,heartbeat):
    heartbeat('authorization_preflight',0)
    root=guard(c)
    if command=='list':
        heartbeat('list',0)
        q=nas_path(root,args.path); entries=[]; truncated=False
        with os.scandir(q) as it:
            for i,e in enumerate(it):
                if i>=args.limit: truncated=True; break
                if e.is_symlink(): fail('nas_symlink_refused')
                entries.append({'name':e.name,'directory':e.is_dir(follow_symlinks=False),'size':e.stat(follow_symlinks=False).st_size if e.is_file(follow_symlinks=False) else None})
                heartbeat('list_entry',i+1)
        emit({'entries':entries,'truncated':truncated}); return
    if command=='read':
        heartbeat('read',args.offset)
        q=nas_path(root,args.path)
        with os.fdopen(open_nofollow(q,os.O_RDONLY),'rb') as f: f.seek(args.offset); data=f.read(args.max_bytes)
        if args.as_base64: val=base64.b64encode(data).decode()
        else:
            try: val=data.decode('utf-8')
            except UnicodeDecodeError: fail('binary_requires_base64')
        emit({'data':val,'encoding':'base64' if args.as_base64 else 'utf-8','next_offset':args.offset+len(data)}); return
    if command=='hash':
        heartbeat('hash',0)
        q=nas_path(root,args.path); before=identity(q); n,h=hash_file(q,heartbeat)
        if identity(q)!=before: fail('source_changed')
        emit({'bytes':n,'sha256':h}); return
    if command=='probe':
        heartbeat('probe_create',0)
        d=nas_path(root,args.path); q=d/('.readiz-nas-probe-'+uuid.uuid4().hex); data=os.urandom(128)
        fd=os.open(q,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        st=q.lstat(); heartbeat('probe_verify',128)
        try:
            actual=q.read_bytes()
            if hashlib.sha256(actual).digest()!=hashlib.sha256(data).digest() or len(actual)!=len(data) or q.lstat().st_ino!=st.st_ino: fail('probe_verification_failed')
        finally:
            try:
                if q.lstat().st_ino==st.st_ino and q.lstat().st_dev==st.st_dev: q.unlink()
            except OSError: pass
        emit({'status':'verified_and_removed','bytes':128}); return
    if command=='copy': copy_worker(c,args,root,heartbeat); return

def parser():
    a=argparse.ArgumentParser(); s=a.add_subparsers(dest='cmd',required=True)
    s.add_parser('status'); l=s.add_parser('list'); l.add_argument('path'); l.add_argument('--limit',type=int,default=100)
    r=s.add_parser('read'); r.add_argument('path'); r.add_argument('--offset',type=int,default=0); r.add_argument('--max-bytes',type=int,default=1048576); r.add_argument('--base64',dest='as_base64',action='store_true')
    s.add_parser('hash').add_argument('path'); s.add_parser('probe').add_argument('path')
    cp=s.add_parser('copy'); cp.add_argument('source'); cp.add_argument('dest'); cp.add_argument('--resume',action='store_true')
    return a

def write_lock(fd,obj):
    data=json.dumps(obj,sort_keys=True).encode()
    os.lseek(fd,0,os.SEEK_SET); os.ftruncate(fd,0); os.write(fd,data); os.fsync(fd)

def acquire_lock(path):
    path=Path(path)
    guard=path.with_name(path.name+'.guard')
    if guard.is_symlink(): fail('unsafe_active_child_marker')
    try: gfd=os.open(guard,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    except OSError: fail('active_child_lock_unavailable')
    try:
        gst=os.fstat(gfd)
        if not stat.S_ISREG(gst.st_mode) or gst.st_uid!=os.getuid() or gst.st_mode&0o077: fail('unsafe_active_child_marker')
        try: fcntl.flock(gfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: fail('active_cli_busy',marker=str(path))
        if path.exists() or path.is_symlink():
            if path.is_symlink(): fail('unsafe_active_child_marker')
            try:
                st=path.stat()
                if st.st_uid!=os.getuid() or st.st_mode&0o077: fail('unsafe_active_child_marker')
                info=json.loads(path.read_text()); owner=int(info.get('owner_pid',0)); child=int(info.get('child_pid') or 0)
                for pid in (owner,child):
                    if pid:
                        try: os.kill(pid,0)
                        except ProcessLookupError: continue
                        except PermissionError: fail('previous_child_state_unknown',pid=pid)
                        else: fail('previous_child_still_running',pid=pid,marker=str(path))
                path.unlink()
            except NasError: raise
            except Exception: fail('previous_child_state_unknown',marker=str(path))
        try: fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        except FileExistsError: fail('active_cli_busy',marker=str(path))
        os.write(fd,json.dumps({'owner_pid':os.getpid(),'child_pid':None,'started_at':time.time()}).encode()); os.fsync(fd)
        st=os.fstat(fd)
        return fd,(st.st_dev,st.st_ino)
    finally:
        try: fcntl.flock(gfd,fcntl.LOCK_UN)
        finally: os.close(gfd)

def release_lock(path,fd,identity):
    os.close(fd)
    try:
        st=os.stat(path,follow_symlinks=False)
        if (st.st_dev,st.st_ino)==identity: os.unlink(path)
    except OSError: pass

def supervise(cmd,hb,timeout,active_marker,lock_fd=None):
    owns_lock=lock_fd is None
    if lock_fd is None:
        lock_fd,lock_identity=acquire_lock(active_marker)
    out_file=tempfile.TemporaryFile(mode='w+b',dir=str(Path(hb).parent)); err_file=tempfile.TemporaryFile(mode='w+b',dir=str(Path(hb).parent))
    try: p=subprocess.Popen(cmd,env=dict(os.environ,READIZ_NAS_FIXED_LAUNCHER='1'),stdout=out_file,stderr=err_file)
    except Exception as e:
        out_file.close(); err_file.close()
        if owns_lock: release_lock(active_marker,lock_fd,lock_identity)
        raise NasError('worker_start_failed',detail=str(e)[:160])
    write_lock(lock_fd,{'owner_pid':os.getpid(),'child_pid':p.pid,'started_at':time.time(),'heartbeat':str(hb)})
    last=time.monotonic(); last_mtime=None
    while p.poll() is None:
        try:
            st=Path(hb).stat()
            if st.st_mtime_ns!=last_mtime: last=time.monotonic(); last_mtime=st.st_mtime_ns
        except OSError: pass
        if time.monotonic()-last>timeout:
            stage='unknown'
            try: stage=json.loads(Path(hb).read_text()).get('stage','unknown')
            except Exception: pass
            p.terminate()
            try: p.wait(timeout=2); reaped=True
            except subprocess.TimeoutExpired:
                p.kill()
                try: p.wait(timeout=2); reaped=True
                except subprocess.TimeoutExpired: reaped=False
            out_file.close(); err_file.close()
            if owns_lock and reaped: release_lock(active_marker,lock_fd,lock_identity)
            elif owns_lock: os.close(lock_fd)
            return {'timed_out':True,'stage':stage,'reaped':reaped,'pid':p.pid,'stdout':'','stderr':''}
        time.sleep(.05)
    out_file.seek(0); err_file.seek(0)
    out=out_file.read(8*1024*1024+1); err=err_file.read(128*1024+1)
    out_file.close(); err_file.close()
    if owns_lock: release_lock(active_marker,lock_fd,lock_identity)
    if len(out)>8*1024*1024 or len(err)>128*1024: return {'timed_out':False,'returncode':2,'stdout':'','stderr':'worker_output_limit_exceeded'}
    return {'timed_out':False,'returncode':p.returncode,'stdout':out.decode('utf-8','replace'),'stderr':err.decode('utf-8','replace')}

def main():
    args=parser().parse_args(); c=cfg(); relaunch(c)
    if args.cmd=='status':
        auth=preflight(); emit({'status':'ok','mount_metadata':'matched' if mount_check(c) else 'unavailable','authorization':'granted' if auth==0 else 'unavailable','authorization_result':auth if auth in (0,1,2) else 'unsupported_or_unknown','prompt_requested':False}); return
    if args.cmd=='list' and not 1<=args.limit<=500: fail('invalid_limit')
    if args.cmd=='read' and (args.offset<0 or not 0<=args.max_bytes<=1048576): fail('invalid_range')
    authorization=preflight()
    if authorization!=0: fail('authorization_unavailable',authorization_result=authorization if authorization in (1,2) else 'unsupported_or_unknown',nas_io=0,prompt_requested=False)
    state=private_dir(Path(c.get('state_dir',str(Path.home()/'.local/state/readiz-nas'))))
    active=state/'active-child.json'
    lock_fd,lock_identity=acquire_lock(active)
    hb=state/('heartbeat-'+uuid.uuid4().hex+'.json')
    hb.write_text(json.dumps({'stage':'worker_start','bytes':0,'time':time.time()}))
    launcher=c.get('launcher',str(Path.home()/'.local/bin/readiz-nas-python'))
    cmd=[launcher,os.path.realpath(__file__),'--worker',args.cmd,'--args',json.dumps(vars(args)),'--heartbeat',str(hb)]
    try: result=supervise(cmd,hb,float(c.get('no_progress_timeout_seconds',15)),active,lock_fd)
    except Exception:
        release_lock(active,lock_fd,lock_identity)
        try: hb.unlink()
        except OSError: pass
        raise
    if result['timed_out'] and not result['reaped']:
        os.close(lock_fd)
    else:
        release_lock(active,lock_fd,lock_identity)
    if hb.exists() and not result['timed_out']: hb.unlink()
    if result['timed_out']:
        emit({'status':'error','code':'no_progress_timeout','stage':result['stage'],'child_reaped':result['reaped'],'remaining_child_pid':None if result['reaped'] else result['pid'],'heartbeat':str(hb),'active_child_marker':str(active),'resume_receipt':'see local state directory','prompt_requested':False}); return 2
    sys.stdout.write(result['stdout'])
    if result['returncode']: sys.stderr.write(result['stderr'])
    return result['returncode']

def child_main(argv):
    a=argparse.ArgumentParser(); a.add_argument('--worker',required=True); a.add_argument('--args',required=True); a.add_argument('--heartbeat',required=True); x=a.parse_args(argv)
    c=cfg(); relaunch(c); args=argparse.Namespace(**json.loads(x.args)); hb=Path(x.heartbeat)
    def beat(stage,n=0):
        tmp=hb.with_suffix('.tmp'); tmp.write_text(json.dumps({'stage':stage,'bytes':n,'time':time.time()})); os.replace(tmp,hb)
    try: worker(c,x.worker,args,beat)
    except NasError as e: emit({'status':'error','code':e.code,**e.data,'prompt_requested':False}); return 2
    except Exception as e: emit({'status':'error','code':'operation_failed','detail':str(e)[:200],'prompt_requested':False}); return 2
    return 0

if __name__=='__main__':
    if '--worker' in sys.argv: raise SystemExit(child_main(sys.argv[1:]))
    try: raise SystemExit(main())
    except NasError as e: emit({'status':'error','code':e.code,**e.data,'prompt_requested':False}); raise SystemExit(2)
