"""Loopback test client; retries only explicit no-commit 503 or uncertain transport with stable keys."""
import json
import random
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RETRYABLE_ERRORS={"server_busy","queue_wait_timeout","database_busy","server_shutdown"}

class Client:
    def __init__(self,base,timeout=10):
        if not base.startswith("http://127.0.0.1:"):
            raise ValueError("This synthetic test client is restricted to loopback")
        self.base,self.timeout=base,timeout

    def once(self,path,payload=None):
        started=time.perf_counter()
        data=None if payload is None else json.dumps(payload).encode()
        try:
            request=Request(self.base+path,data=data,headers={"Content-Type":"application/json"})
            try:
                response=urlopen(request,timeout=self.timeout)
            except HTTPError as error:
                response=error
            with response:
                status=response.code
                retry_after=response.headers.get("Retry-After")
                body=json.load(response)
            return {"status":status,"body":body,"retry_after":retry_after,
                    "latency_ms":(time.perf_counter()-started)*1000}
        except (URLError,OSError,TimeoutError,HTTPException,ValueError) as exc:
            reason=getattr(exc,"reason",exc)
            detail={"error":type(exc).__name__,"reason_type":type(reason).__name__,
                    "errno":getattr(reason,"errno",None),"winerror":getattr(reason,"winerror",None)}
            return {"status":"transport_error","body":detail,
                    "retry_after":None,"latency_ms":(time.perf_counter()-started)*1000}

    def call(self,path,payload=None,max_attempts=1):
        if not 1<=max_attempts<=6:
            raise ValueError("max_attempts must be 1-6")
        began=time.perf_counter()
        attempts=[]
        for attempt in range(max_attempts):
            result=self.once(path,payload)
            attempts.append(result)
            explicit_retry=(result["status"]==503 and result["body"].get("error") in RETRYABLE_ERRORS
                            and result["body"].get("outcome")=="not_committed")
            uncertain_transport=result["status"]=="transport_error"
            if attempt==max_attempts-1 or not (explicit_retry or uncertain_transport):
                break
            # Server emits integer Retry-After=1; never retry earlier than that value.
            delay=float(result["retry_after"] or min(2,0.25*2**attempt))
            if not 0<=delay<=5:
                break
            time.sleep(delay+random.uniform(0,0.05))
        return {**result,"attempts":attempts,"logical_latency_ms":(time.perf_counter()-began)*1000}

