import os, sys, time, secrets, base64, json, jwt, requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
NAME=os.environ['COINBASE_API_KEY_NAME']; RAW=os.environ['COINBASE_API_PRIVATE_KEY'].strip()
HOST='api.coinbase.com'
def key():
    if RAW.startswith('-----'): return RAW.replace('\\n','\n'),'ES256'
    b=base64.b64decode(RAW); pk=Ed25519PrivateKey.from_private_bytes(b[:32])
    return pk,'EdDSA'
def get(path):
    k,alg=key(); now=int(time.time())
    tok=jwt.encode({'sub':NAME,'iss':'cdp','nbf':now,'exp':now+120,'uri':f'GET {HOST}{path.split("?")[0]}'},k,algorithm=alg,headers={'kid':NAME,'nonce':secrets.token_hex()})
    r=requests.get(f'https://{HOST}{path}',headers={'Authorization':f'Bearer {tok}'},timeout=20)
    return r.status_code, (r.json() if r.headers.get('content-type','').startswith('application/json') else r.text[:300])
if __name__=='__main__':
    for p in sys.argv[1:]: print(p, json.dumps(get(p),indent=1)[:4000])
