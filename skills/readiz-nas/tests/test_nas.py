import concurrent.futures, contextlib, importlib.util, io, json, os, tempfile, threading, unittest
from pathlib import Path

SCRIPT=Path(__file__).parents[1]/'scripts/nas.py'
spec=importlib.util.spec_from_file_location('nas_cli',SCRIPT); nas=importlib.util.module_from_spec(spec); spec.loader.exec_module(nas)

class Args:
    def __init__(self,source,dest,resume=False): self.source,self.dest,self.resume=source,dest,resume

class NasTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.base=Path(self.tmp.name)
        self.mount=self.base/'mount'; self.mount.mkdir(); self.local=self.base/'local'; self.local.mkdir(); self.state=self.base/'state'; self.state.mkdir(mode=0o700)
        self.c={'mount':str(self.mount),'allowed_local_roots':[str(self.local)],'state_dir':str(self.state),'min_local_free_bytes':0}
    def tearDown(self): self.tmp.cleanup()

    def test_denied_preflight_never_calls_mount_or_opens_nas(self):
        for result in (1,2,None,99):
            called=[]
            with self.assertRaises(nas.NasError) as e: nas.guard(self.c,auth=lambda r=result:r,mounted=lambda c:called.append(1))
            self.assertEqual(e.exception.code,'authorization_unavailable'); self.assertEqual(e.exception.data['nas_io'],0); self.assertEqual(called,[])

    def test_mount_source_filesystem_and_write_flags(self):
        self.c['source']='//server/share'
        self.assertTrue(nas.mount_check(self.c,lambda:f"//server/share on {self.mount} (smbfs, rw, mounted by user)"))
        self.assertTrue(nas.mount_check(self.c,lambda:f"//server/share {self.mount} smbfs rw,nodev,nosuid 0 0"))
        self.assertTrue(nas.mount_check(self.c,lambda:f"//server/share on {self.mount} (smbfs, nodev, nosuid, mounted by readiz)"))
        self.assertFalse(nas.mount_check(self.c,lambda:f"//wrong/share on {self.mount} (smbfs, rw)"))
        self.assertFalse(nas.mount_check(self.c,lambda:f"//server/share on {self.mount} (smbfs, read-only)"))

    def test_mount_escape_and_symlink_rejected(self):
        with self.assertRaises(nas.NasError): nas.nas_path(self.mount,'../outside')
        target=self.base/'target'; target.mkdir(); (self.mount/'link').symlink_to(target)
        with self.assertRaises(nas.NasError): nas.nas_path(self.mount,'link/file')

    def test_copy_verified_and_existing_destination_preserved(self):
        src=self.local/'source.bin'; src.write_bytes(os.urandom(12000)); dest='nas:out.bin'
        with contextlib.redirect_stdout(io.StringIO()): nas.copy_worker(self.c,Args(str(src),dest),self.mount,lambda *_:None)
        out=self.mount/'out.bin'; self.assertEqual(out.read_bytes(),src.read_bytes())
        with self.assertRaises(nas.NasError): nas.copy_worker(self.c,Args(str(src),dest),self.mount,lambda *_:None)
        self.assertEqual(out.read_bytes(),src.read_bytes())

    def test_copy_refuses_foreign_reservation_without_removing_it(self):
        src=self.local/'source.bin'; src.write_bytes(b'payload')
        reservation=self.mount/'.out.bin.readiz-reservation'; reservation.write_bytes(b'owned elsewhere')
        with self.assertRaises(FileExistsError): nas.copy_worker(self.c,Args(str(src),'nas:out.bin'),self.mount,lambda *_:None)
        self.assertEqual(reservation.read_bytes(),b'owned elsewhere'); self.assertFalse((self.mount/'out.bin').exists())

    def test_nas_to_local_requires_allowed_root_and_verifies(self):
        src=self.mount/'input.bin'; src.write_bytes(os.urandom(2300)); dst=self.local/'output.bin'
        with contextlib.redirect_stdout(io.StringIO()): nas.copy_worker(self.c,Args('nas:input.bin',str(dst)),self.mount,lambda *_:None)
        self.assertEqual(dst.read_bytes(),src.read_bytes())
        with self.assertRaises(nas.NasError): nas.local_path(str(self.base/'elsewhere'),self.c)
        with self.assertRaises(nas.NasError): nas.local_path(str(self.local/'..'/'outside'),self.c)

    def test_copy_requires_explicit_local_and_nas_namespaces(self):
        src=self.local/'source.bin'; src.write_bytes(b'x')
        with self.assertRaises(nas.NasError) as e: nas.copy_worker(self.c,Args(str(src),'bare-destination'),self.mount,lambda *_:None)
        self.assertEqual(e.exception.code,'copy_requires_one_local_and_one_nas_path')

    def test_profile_limits_reject_unknown_and_negative_values(self):
        base={'mount':str(self.mount),'source':'//example.invalid/share','allowed_local_roots':[str(self.local)]}
        self.assertIs(nas.validate_config(base),base)
        for invalid in ({**base,'max_local_copy_bytes':-1},{**base,'min_local_free_bytes':-1},{**base,'no_progress_timeout_seconds':-1},{**base,'typo_option':True}):
            with self.assertRaises(nas.NasError): nas.validate_config(invalid)

    def test_interrupted_copy_checkpoint_resume_and_source_change(self):
        src=self.local/'big.bin'; src.write_bytes(os.urandom(3*nas.CHUNK+17)); dest='nas:big.bin'; calls=[0]
        def interrupt(stage,n):
            if stage=='copy':
                calls[0]+=1
                if calls[0]==1: raise RuntimeError('simulated stop')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError): nas.copy_worker(self.c,Args(str(src),dest),self.mount,interrupt)
        receipts=list(self.state.glob('*.json')); self.assertEqual(len(receipts),1)
        rec=json.loads(receipts[0].read_text()); self.assertEqual(rec['status'],'incomplete'); self.assertEqual(rec['bytes'],nas.CHUNK)
        with contextlib.redirect_stdout(io.StringIO()): nas.copy_worker(self.c,Args(str(src),dest,True),self.mount,lambda *_:None)
        self.assertEqual((self.mount/'big.bin').read_bytes(),src.read_bytes())

    def test_interrupted_finalization_resumes_reserved_destination(self):
        src=self.local/'big.bin'; src.write_bytes(os.urandom(3*nas.CHUNK+3)); calls=[0]
        def interrupt(stage,n):
            if stage=='finalize_destination':
                calls[0]+=1
                if calls[0]==1: raise RuntimeError('pause after destination reservation')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError): nas.copy_worker(self.c,Args(str(src),'nas:big.bin'),self.mount,interrupt)
        dst=self.mount/'big.bin'; self.assertTrue(dst.exists()); before=dst.lstat().st_ino
        with contextlib.redirect_stdout(io.StringIO()): nas.copy_worker(self.c,Args(str(src),'nas:big.bin',True),self.mount,lambda *_:None)
        self.assertEqual(dst.read_bytes(),src.read_bytes()); self.assertEqual(dst.lstat().st_ino,before)

    def test_resume_rejects_changed_source(self):
        src=self.local/'big.bin'; src.write_bytes(os.urandom(2*nas.CHUNK)); calls=[0]
        def interrupt(stage,n):
            if stage=='copy' and not calls[0]: calls[0]=1; raise RuntimeError()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError): nas.copy_worker(self.c,Args(str(src),'nas:big.bin'),self.mount,interrupt)
        src.write_bytes(b'changed'+src.read_bytes()[7:])
        with self.assertRaises(nas.NasError) as e: nas.copy_worker(self.c,Args(str(src),'nas:big.bin',True),self.mount,lambda *_:None)
        self.assertEqual(e.exception.code,'source_changed_or_receipt_mismatch')

    def test_supervisor_times_out_only_without_progress(self):
        hb=self.base/'hb'; hb.write_text(json.dumps({'stage':'blocked_open'}))
        marker=self.base/'active.json'
        stalled=['import time; time.sleep(2)']
        result=nas.supervise([os.sys.executable,'-c',stalled[0]],hb,.2,marker)
        self.assertTrue(result['timed_out']); self.assertTrue(result['reaped']); self.assertEqual(result['stage'],'blocked_open')
        active="import json,os,time,pathlib; p=pathlib.Path(%r);\nfor i in range(8): p.write_text(json.dumps({'stage':'stream','bytes':i})); time.sleep(.08)" % str(hb)
        hb.write_text(json.dumps({'stage':'start'}))
        result=nas.supervise([os.sys.executable,'-c',active],hb,.2,marker)
        self.assertFalse(result['timed_out']); self.assertEqual(result['returncode'],0)
        voluminous="import json,sys; sys.stdout.write(json.dumps({'data':'\\0'*1048576}))"
        result=nas.supervise([os.sys.executable,'-c',voluminous],hb,.2,marker)
        self.assertFalse(result['timed_out']); self.assertEqual(len(result['stdout']),len(json.dumps({'data':'\0'*1048576})))

    def test_active_cli_lock_is_exclusive(self):
        marker=self.state/'active-child.json'; fd,ident=nas.acquire_lock(marker)
        try:
            with self.assertRaises(nas.NasError): nas.acquire_lock(marker)
        finally: nas.release_lock(marker,fd,ident)

    def test_concurrent_lock_claim_has_one_winner(self):
        marker=self.state/'contended.json'; barrier=threading.Barrier(6); acquired=[]; failures=[]; held=threading.Event()
        def claim():
            barrier.wait()
            try:
                fd,ident=nas.acquire_lock(marker); acquired.append((fd,ident)); held.wait(1)
            except nas.NasError as e: failures.append(e.code)
        threads=[threading.Thread(target=claim) for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join(.5)
        held.set()
        for t in threads: t.join()
        for fd,ident in acquired: nas.release_lock(marker,fd,ident)
        self.assertEqual(len(acquired),1); self.assertEqual(len(failures),5)

    def test_install_conflict_preserves_existing_file(self):
        from importlib.util import spec_from_file_location, module_from_spec
        p=SCRIPT.parent/'install.py'; s=spec_from_file_location('nas_install',p); mod=module_from_spec(s); s.loader.exec_module(mod)
        dest=self.base/'conflict'; dest.write_bytes(b'keep')
        with self.assertRaises(RuntimeError): mod.put(dest,b'replace',0o600)
        self.assertEqual(dest.read_bytes(),b'keep')
        link=self.base/'linked'; link.symlink_to(dest)
        with self.assertRaises(RuntimeError): mod.put(link,b'bad',0o600)

    def test_installer_creates_private_paths_and_is_idempotent(self):
        from importlib.util import spec_from_file_location, module_from_spec
        p=SCRIPT.parent/'install.py'; s=spec_from_file_location('nas_install_flow',p); mod=module_from_spec(s); s.loader.exec_module(mod)
        home=self.base/'fake-home'; home.mkdir(mode=0o700)
        launcher=self.base/'fixed-python'; launcher.write_text('#!/bin/sh\nexit 0\n'); launcher.chmod(0o700)
        profile=self.base/'profile.json'; profile.write_text(json.dumps({'mount':str(self.mount),'source':'//server/share','launcher':str(launcher),'allowed_local_roots':[str(self.local)]})); profile.chmod(0o600)
        a=mod.install(profile,SCRIPT.parent.parent,home)
        b=mod.install(profile,SCRIPT.parent.parent,home)
        self.assertEqual(a['wrapper'],b['wrapper'])
        conf=home/'.config/readiz-nas/access.json'; wrapper=home/'.local/bin/readiz-nas'
        self.assertEqual(conf.stat().st_mode & 0o777,0o600); self.assertEqual(wrapper.stat().st_mode & 0o777,0o700)
        self.assertEqual((home/'.config/readiz-nas').stat().st_mode & 0o777,0o700)

if __name__=='__main__': unittest.main()
