"""Test-only value parity in fingerprint-test child interpreters; no recorder switch."""
import atexit
import hashlib
import json
import os
from pathlib import Path
import threading


def install(directory):
    from ldr import fingerprint as fp
    if getattr(fp,'SIGNATURE_VERSION',1)!=2 or getattr(fp,'_native_reader',None) is None:
        return  # Intentional old-format/reference checkouts cannot use the v2 extension.
    from ldr.native_bridge import Reader
    import ldr._native as module
    original=fp._fingerprint_details
    local=threading.local()
    report={'values':0,'nodes':0,'failures':[]}
    class Wire:
        def __init__(self): self.data=bytearray()
        def update(self,data): self.data.extend(data)
    def details(value,budget,partial_ok,sketch=False):
        engine=getattr(local,'engine',None)
        if engine is None: engine=local.engine=Reader(module)
        wire=Wire();walk=fp._Walk(budget,partial_ok)
        normalized=fp.STATS.tmp_normalized
        status='complete'
        try:
            try: walk.encode(value,wire)
            except fp._Stop as error: status=error.status
            except RecursionError: status=fp.UNKNOWN
            except Exception: status=fp.FAILED
            expected={'bytes':wire.data,'digest':hashlib.blake2b(wire.data,digest_size=16).hexdigest(),
                      'visited':walk.visited,'status':status,'partial':walk.partial,'immutable':walk.immutable,
                      'normalized':fp.STATS.tmp_normalized-normalized,
                      'over_ceiling':budget.max_nodes is not None and walk.visited>budget.max_nodes}
            actual=engine.walk(value,budget,partial_ok)
            differences=[key for key in expected if actual[key]!=expected[key]]
            if differences:
                report['failures'].append({'type':fp.type_identity(type(value)),'fields':differences})
            report['values']+=1;report['nodes']+=walk.visited
        except BaseException as error:
            report['failures'].append({'type':fp.type_identity(type(value)),'error':type(error).__name__})
        finally:
            fp.STATS.tmp_normalized=normalized
        return original(value,budget,partial_ok,sketch)
    fp._fingerprint_details=details
    def save():
        Path(directory,f'{os.getpid()}.json').write_text(json.dumps(report),encoding='utf8')
    atexit.register(save)
