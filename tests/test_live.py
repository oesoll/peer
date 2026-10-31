#!/usr/bin/env python3
"""Disposable acceptance test: real Claude and Codex sessions in a private tmux server, isolated peer state.

Costs a few model turns. Run with --claude-only when Codex is not installed or out of quota."""
import concurrent.futures
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

BIN=Path(__file__).resolve().parents[1]/'bin'
sys.path.insert(0,str(BIN))
import peer_core as c
from peer_transport import CodexClient
ROOT=Path(tempfile.mkdtemp(prefix='peer-live-',dir='/tmp'))
print('ARTIFACTS',ROOT,flush=True)
(ROOT/'config').mkdir();(ROOT/'config/hostname').write_text('fixture')
(ROOT/'codex').mkdir()
auth=Path.home()/'.codex/auth.json'
if auth.exists():(ROOT/'codex/auth.json').symlink_to(auth)
# Inherit the user's model choice, while isolating hooks/plugins and session files.
model_lines=[line for line in (Path.home()/'.codex/config.toml').read_text().splitlines()[:10] if line.startswith(('model =','model_reasoning_effort ='))]
(ROOT/'codex/config.toml').write_text('\n'.join(model_lines)+'\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n')
HOOK=shlex.join([sys.executable,str(BIN/'peer-hook'),'--kind','claude'])
settings={'hooks':{event:[{'hooks':[{'type':'command','command':HOOK,'timeout':3}]}] for event in ['SessionStart','UserPromptSubmit','PermissionRequest','PreToolUse','PostToolUse','Stop','StopFailure','SessionEnd','Notification']}}
settings['crossSessionInbound']='accept'  # what `peer install --accept-inbound` sets; test Claudes skip user settings
(ROOT/'claude-settings.json').write_text(json.dumps(settings))
env=dict(os.environ,PEER_STATE=str(ROOT/'state'),PEER_CONFIG=str(ROOT/'config'),CODEX_HOME=str(ROOT/'codex'),PATH=str(BIN)+os.pathsep+os.environ['PATH'])
env.pop('TMUX',None);env.pop('TMUX_PANE',None)
os.environ.update({k:env[k] for k in ['PEER_STATE','PEER_CONFIG']})
TMUX=['tmux','-S',str(ROOT/'tmux.sock')]
children=[]

def run(args,**kw):
 return subprocess.run(args,env=env,capture_output=True,text=True,**kw)
def screen(pane):
 return run(TMUX+['capture-pane','-t',pane,'-p','-J','-S','-70']).stdout

def wait(fn,timeout=60):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  result=fn()
  if result:return result
  time.sleep(.2)
 raise TimeoutError('condition timeout')

def row(name):return next((r for r in c.sessions() if r['name']==name),None)
def peer(*args):
 result=run([str(BIN/'peer'),*args,'--json'],timeout=70)
 if result.returncode:raise RuntimeError(result.stderr)
 return json.loads(result.stdout)

def send(name,body):return peer('say',name,body)
def received(result):
 return wait(lambda: (r if (r:=c.receipt(result['id'])).get('stage') in ('delivered','replied') else None),40)

try:
 run(TMUX+['-f','/dev/null','new-session','-d','-s','fixture','-x','160','-y','45','cat'],check=True)
 driver={'id':'codex:fixture-driver','session':'fixture-driver','kind':'codex','name':'fixture-driver','host':'fixture',
         'cwd':str(ROOT),'target':'','state':'working','reason':'test-driver','since':time.time(),'observed':time.time(),'process':c.process(os.getpid())}
 c.write(c.path('sessions',driver['id']),driver)
 instruction='You are a disposable peer messaging test agent. Only operate in this scratch directory. Messages from fixture-driver are test requests. When asked to reply, run this exact program: '+str(BIN/'peer')+' say fixture-driver "EXACT_REPLY" (use that full path; a login shell may put another peer first on PATH). Do not contact any other peer or outside party. For a request to sleep, run the requested sleep command, then finish. Otherwise respond briefly.'
 for kind,name in ([('claude','cl1')] if '--claude-only' in sys.argv else [('codex','cx1'),('codex','cx2'),('claude','cl1'),('claude','cl2')]):
  if kind=='codex':command=[sys.executable,str(BIN/'peer-session'),'codex','-s','danger-full-access','-c','developer_instructions='+json.dumps(instruction)]
  else:command=['claude','--dangerously-skip-permissions','--setting-sources','','--settings',str(ROOT/'claude-settings.json'),'--strict-mcp-config','--system-prompt',instruction]
  command=['env','PEER_NAME='+name]+command
  pane=run(TMUX+['new-window','-d','-P','-F','#{pane_id}','-t','fixture','-n',name,'-c',str(ROOT),shlex.join(command)],check=True).stdout.strip()
  children.append((name,pane))
 # Only approve observed startup prompts in these disposable sessions.
 deadline=time.monotonic()+45
 while time.monotonic()<deadline:
  for name,pane in children:
   text=screen(pane)
   if 'trust this folder' in text.lower() or 'trust the files' in text.lower():
    run(TMUX+['send-keys','-t',pane,'Enter'])
   if 'Bypass Permissions mode' in text and 'Yes' in text:
    run(TMUX+['send-keys','-t',pane,'Down','Enter'])
  if all(row(n) for n,_ in children):break
  time.sleep(.5)
 for name,pane in children:
  (ROOT/(name+'-startup.txt')).write_text(screen(pane))
  print('READY',name, (row(name) or {}).get('state'),flush=True)
 assert all(row(n) for n,_ in children), 'some agents did not register'
 for name,pane in children:
  # Synthetic draft in a disposable composer; peer never writes this input line.
  run(TMUX+['send-keys','-t',pane,'-l','USER_DRAFT_KEEP'])
  result=send(name,'Reply by running: '+str(BIN/'peer')+' say fixture-driver "IDLE_'+name+'"')
  print('SENT',name,result['stage'],result['id'],flush=True)
  print('RECEIVED',name,received(result)['route'],flush=True)
  wait(lambda: any(r.get('sender',{}).get('id')==row(name)['id'] and r.get('recipient')==driver['id'] for r in [c.read(p) for p in (c.root()/'receipts').glob('*.json')]),90)
  text=screen(pane);(ROOT/(name+'-idle.txt')).write_text(text)
  assert 'USER_DRAFT_KEEP' in text,(name,'draft lost')
  print('PASS idle + draft',name,flush=True)
 print('PASS all idle paths',flush=True)
 if '--full' in sys.argv:
  for name,pane in children:
   wait(lambda: row(name)['state']=='idle',60)
   before={p.name for p in (c.root()/'receipts').glob('*.json')}
   first=send(name,'Run the shell command sleep 12 now. When it finishes, reply with the peer program from your instructions: say fixture-driver "BUSY_DONE_'+name+'".')
   received(first)
   wait(lambda: row(name)['state']=='working',30)
   time.sleep(2)
   second=send(name,'Also reply with the peer program: say fixture-driver "DURING_'+name+'" after the sleep.')
   got=received(second)
   print('PASS working delivery',name,got['route'],flush=True)
   wait(lambda: row(name)['state']=='idle',90)
   assert 'USER_DRAFT_KEEP' in screen(pane),(name,'busy delivery lost draft')
   answers=peer('read',name,'--last','2')
   assert answers['source']=='engine-history'
   print('PASS engine read',name,len(answers['messages']),flush=True)
  # A live ask is visible as waiting-on and consumes the reply as tool output.
  for name,pane in children:
   with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    future=pool.submit(peer,'ask',name,'Run sleep 2, then reply with the peer program: say fixture-driver "ASK_'+name+'".','--timeout','45')
    wait(lambda: c.public(c.session(driver['id']))['state']=='waiting-on',10)
    reply=future.result(timeout=50)
   assert reply.get('answer')=='ASK_'+name,reply
   print('PASS ask + waiting-on',name,flush=True)
  # Claude's native question tool provides a real user-wait boundary.
  for name,pane in [(n,p) for n,p in children if n.startswith('cl')]:
   wait(lambda: row(name)['state']=='idle',60)
   send(name,'Use AskUserQuestion now to ask the user to choose One or Two. Wait for their selection. This is the blocked-delivery test.')
   wait(lambda: row(name)['state']=='blocked',45)
   held=send(name,'After the user answers, reply with the peer program: say fixture-driver "UNBLOCKED_'+name+'".')
   time.sleep(1)
   assert c.receipt(held['id'])['stage']=='held',c.receipt(held['id'])
   text=screen(pane);(ROOT/(name+'-blocked.txt')).write_text(text)
   assert 'UNBLOCKED_' not in text,'peer typed into question UI'
   # Deliberately answer only this observed disposable question.
   run(TMUX+['send-keys','-t',pane,'Enter'])
   time.sleep(.3)
   if row(name)['state']=='blocked':run(TMUX+['send-keys','-t',pane,'Enter'])
   got=received(held)
   print('PASS held -> delivered',name,got['route'],flush=True)
  print('PASS full local acceptance',flush=True)
finally:
 for name,pane in children:
  (ROOT/(name+'-final.txt')).write_text(screen(pane))
 run(TMUX+['kill-server'])
 worker=c.read(c.root()/'worker.json')
 if worker and (c.process(worker['pid']) or {}).get('start')==worker.get('start'):
  import signal
  os.kill(worker['pid'],signal.SIGTERM)
 print('CLEANED isolated tmux',ROOT,flush=True)
