"""Loop events straight from Redis (vera:loop:events:<sid>) - never the SSE reattach, which
returns nothing when prod is restarting (4 Oct 2026). The Redis URL comes from CENSUS_REDIS_URL,
else from the prod process on :8999 (one we can read), else localhost."""
import json,subprocess,sys,collections,os
try:
    import redis
except ImportError:        # pragma: no cover - needs redis-py (the langchain venv)
    redis = None
def _redis_url():
    """REDIS_URL of prod (the :8999 process). Only processes we can read: a setsid shell
    has no /usr/sbin, and pgrep also matches other users' containers (4 Oct: EACCES)."""
    if os.environ.get("CENSUS_REDIS_URL"):
        return os.environ["CENSUS_REDIS_URL"]
    cands=[]
    try:
        cands+=subprocess.run("(/usr/bin/ss -ltnp 'sport = :8999' 2>/dev/null || /usr/sbin/ss -ltnp 'sport = :8999' 2>/dev/null) | grep -o 'pid=[0-9]*' | cut -d= -f2",shell=True,capture_output=True,text=True).stdout.split()
    except Exception:
        pass
    try:
        cands+=subprocess.run(["pgrep","-u",str(os.getuid()),"-f","Vera.vera.capability_orchestration"],capture_output=True,text=True).stdout.split()
    except Exception:          # no pgrep here (a test container)
        pass
    for pid in dict.fromkeys(cands):
        try:
            env=open(f"/proc/{pid}/environ","rb").read().split(b"\0")
        except OSError:
            continue
        url=[e.split(b"=",1)[1].decode() for e in env if e.startswith(b"REDIS_URL=")]
        if url:
            return url[0]
    return "redis://localhost:6379/0"
def rconn():
    return redis.Redis.from_url(_redis_url())
def events(sid):
    out=[]
    for raw in rconn().lrange("vera:loop:events:%s"%sid,0,-1):
        try: out.append(json.loads(raw))
        except Exception: pass
    return out
if __name__=="__main__":
    ev=events(sys.argv[1]); T=lambda e:str(e.get("type",""))
    print("events",len(ev), dict(collections.Counter(T(e).split(".",1)[-1] for e in ev).most_common(12)))
    for e in ev:
        t=T(e).split(".",1)[-1]
        if t in ("plan","step_start","verify","step_critique","tool_done","done","arg_correction","error","cancelled","stalled") or "error" in t:
            s={k:e.get(k) for k in ("step_id","tool","ok","met","title","reason","note","error","summary","elapsed_ms") if e.get(k) not in (None,"")}
            print(str(e.get("ts",""))[11:19],t,json.dumps(s)[:230])
