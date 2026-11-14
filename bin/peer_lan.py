"""Machines on the same network: pair once, then find each other and talk over TLS, without ssh.

Pairing is the only trust step, and it buys messages only (MESSAGE_OPS): a paired machine can find agents
here and talk to them, not start them, run commands or touch files, which ssh and relay carry for someone
who already has a shell here. Nothing unpaired is answered, and being on the same Wi-Fi proves nothing; in
a cafe the person at the next table is on it too.

Pairing compares a six-digit number on both screens, like Bluetooth. Each side commits to a random value
before seeing the other's, so someone in between cannot steer both screens to the same number.
Discovery uses what the system already has (dns-sd on macOS, avahi on Linux); when the network does not
carry it (a VPN, a guest network), pair by address and that address is kept.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time

from peer_core import config, host, lock, read, write

PORT = int(os.environ.get('PEER_LAN_PORT') or 47231)
SERVICE = '_peer._tcp'
PAIRING = '_peerpair._tcp'
LIMIT = 1 << 20  # one request line; messages are far smaller
# Pairing proves a machine on this network, not a person with a shell here, so it gets messages and
# nothing that runs or reads things (spawn, read, sm run, sm cp). A new operation is refused until listed.
MESSAGE_OPS = frozenset({'ping', 'list', 'receive', 'receipt'})


def folder():
    return config() / 'lan'


def identity():
    """This machine's key and self-signed certificate, made once with the system openssl."""
    path = folder()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    key, cert = path / 'key.pem', path / 'cert.pem'
    if not cert.exists():
        ext = path / 'cert.cnf'
        ext.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=peer\n'
                       '[ext]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyCertSign\n')
        try:
            subprocess.run(['openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', str(key)],
                           check=True, capture_output=True)
            os.chmod(key, 0o600)
            subprocess.run(['openssl', 'req', '-new', '-x509', '-key', str(key), '-out', str(cert), '-days', '3650',
                            '-config', str(ext), '-subj', '/CN=peer-' + host()], check=True, capture_output=True)
        except FileNotFoundError:
            raise ValueError('pairing needs openssl on PATH')
        finally:
            ext.unlink(missing_ok=True)
    return key, cert


def der(pem):
    return ssl.PEM_cert_to_DER_cert(pem)


def paired():
    """{name: {cert, address, fixed}} of machines paired with this one."""
    return read(folder() / 'paired.json')


def remember(name, **fields):
    file = folder() / 'paired.json'
    with lock(file):
        rows = read(file)
        rows[name] = dict(rows.get(name) or {}, **fields)
        write(file, rows)


def forget(name):
    file = folder() / 'paired.json'
    with lock(file):
        rows = read(file)
        if rows.pop(name, None) is None:
            raise ValueError('not paired with ' + name + '; peer pair --list')
        write(file, rows)


def owner(der_bytes, known):
    return next((name for name, row in known.items() if der(row['cert']) == der_bytes), None)


def server_context(trust_paired=True):
    key, cert = identity()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert), str(key))
    known = ''.join(row['cert'] for row in paired().values())
    if trust_paired and known:
        context.load_verify_locations(cadata=known)
        context.verify_mode = ssl.CERT_REQUIRED
    return context


def client_context(server_pem=None):
    key, cert = identity()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False  # identity is the pinned certificate, not a name
    if server_pem:
        context.load_verify_locations(cadata=server_pem)
    else:
        context.verify_mode = ssl.CERT_NONE  # pairing: the compared numbers are the check
    context.load_cert_chain(str(cert), str(key))
    return context


def send(stream, value):
    stream.write(json.dumps(value).encode() + b'\n')
    stream.flush()


def receive(stream):
    line = stream.readline(LIMIT)
    if not line:
        raise ConnectionError('the other machine closed the connection')
    return json.loads(line)


def number(cert_a, cert_b, nonce_a, nonce_b):
    digest = hashlib.sha256(der(cert_a) + der(cert_b) + bytes.fromhex(nonce_a) + bytes.fromhex(nonce_b)).digest()
    return '%06d' % (int.from_bytes(digest[:8], 'big') % 1000000)


def commitment(cert, nonce):
    return hashlib.sha256(der(cert) + bytes.fromhex(nonce)).hexdigest()


# --- pairing ------------------------------------------------------------------------------------------

def pair_listen(confirm, out=print, timeout=120):
    """Wait for another machine's `peer pair`, then compare numbers. Returns its name, or None."""
    mine = identity()[1].read_text()
    try:  # next to the serve port, so one firewall rule covers both; any free port if it is taken
        listener = socket.create_server(('', PORT + 1))
    except OSError:
        listener = socket.create_server(('', 0))
    with listener:
        port = listener.getsockname()[1]
        listener.settimeout(timeout)
        ad = advertise(host(), PAIRING, port)
        try:
            out('Waiting up to %d s. On the other machine run: peer pair   (no discovery there? peer pair %s:%d)'
                % (timeout, local_address(), port))
            conn, address = listener.accept()
        except socket.timeout:
            raise ValueError('no machine came to pair within %d s' % timeout)
        finally:
            stop(ad)
    with server_context(trust_paired=False).wrap_socket(conn, server_side=True) as tls:
        tls.settimeout(timeout)
        stream = tls.makefile('rwb')
        nonce = secrets.token_hex(16)
        send(stream, dict(host=host(), port=PORT, commit=commitment(mine, nonce)))
        other = receive(stream)
        send(stream, dict(nonce=nonce))
        shown = number(mine, other['cert'], nonce, other['nonce'])
        return finish(stream, other['host'], other['cert'], '%s:%d' % (address[0], int(other.get('port') or PORT)),
                      bool(other.get('fixed')), shown, confirm, out)


def pair_join(address, port, confirm, out=print, fixed=False, timeout=120):
    """Connect to a machine waiting in `peer pair` and compare numbers. Returns its name, or None."""
    mine = identity()[1].read_text()
    with socket.create_connection((address, port), timeout=10) as raw:
        with client_context().wrap_socket(raw) as tls:
            tls.settimeout(timeout)
            theirs = ssl.DER_cert_to_PEM_cert(tls.getpeercert(binary_form=True))
            stream = tls.makefile('rwb')
            hello = receive(stream)
            nonce = secrets.token_hex(16)
            send(stream, dict(host=host(), port=PORT, cert=mine, nonce=nonce, fixed=fixed))
            reveal = receive(stream)
            if commitment(theirs, reveal['nonce']) != hello['commit']:
                raise ValueError('pairing check failed: the other side changed its answer midway; nothing saved')
            shown = number(theirs, mine, reveal['nonce'], nonce)
            return finish(stream, hello['host'], theirs, '%s:%d' % (address, int(hello.get('port') or PORT)),
                          fixed, shown, confirm, out)


def finish(stream, name, cert, address, fixed, shown, confirm, out):
    if name == host():
        raise ValueError('both machines are named %s; give one another name in %s' % (name, config() / 'hostname'))
    out('Pairing with %s. Number: %s %s' % (name, shown[:3], shown[3:]))
    agreed = bool(confirm(name, shown))
    try:
        send(stream, dict(ok=agreed))
        theirs = bool(receive(stream).get('ok'))
    except (OSError, ConnectionError):
        theirs = False  # the other side quit without answering
    if not (agreed and theirs):
        out('Not paired: %s did not confirm.' % ('this machine' if not agreed else name))
        return None
    remember(name, cert=cert, address=address, fixed=fixed, paired=time.time())
    out('Paired with %s. Its agents are reachable by name while `peer serve` runs on both machines.' % name)
    return name


def pairing_offers(seconds=3):
    """{name: address:port} of machines waiting in `peer pair` on this network, not counting this one."""
    return {name: where for name, where in browse(PAIRING, seconds).items() if name != host()}


# --- serving and calling ------------------------------------------------------------------------------

def serve(rpc, port=PORT, stopped=None, ready=None):
    """Answer paired machines' requests with rpc(request, their name) until `stopped` is set."""
    with socket.create_server(('', port)) as listener:
        listener.settimeout(.5)
        ad = advertise(host(), SERVICE, port)
        if ready:
            ready.set()
        try:
            while not (stopped and stopped.is_set()):
                try:
                    conn, address = listener.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=answer, args=(conn, address, rpc), daemon=True).start()
        finally:
            stop(ad)


def answer(conn, address, rpc):
    known = paired()
    if not known:
        conn.close()
        return
    try:
        with server_context().wrap_socket(conn, server_side=True) as tls:
            tls.settimeout(30)
            stream = tls.makefile('rwb')
            name = owner(tls.getpeercert(binary_form=True), known)
            request = receive(stream)
            if not name:
                send(stream, dict(error='not paired with ' + host()))
                return
            op = request.get('op') if isinstance(request, dict) else None
            if not (isinstance(op, str) and op in MESSAGE_OPS):
                send(stream, dict(error='%s takes only messages from paired machines, not %s' % (host(), str(op)[:40])))
                return
            row = known[name]
            if not row.get('fixed') and row.get('address', '').rsplit(':', 1)[0] != address[0]:
                remember(name, address='%s:%s' % (address[0], row.get('address', ':%d' % PORT).rsplit(':', 1)[1]))
            try:
                send(stream, dict(result=rpc(request, name)))
            except Exception as error:  # the caller gets the reason, like ssh's stderr
                send(stream, dict(error=str(error)[:500]))
    except (OSError, ssl.SSLError, ValueError):
        pass  # an unpaired or broken client: say nothing more


def call(name, request, timeout=8):
    """Run one peer operation on a paired machine."""
    row = paired().get(name)
    if not row:
        raise ValueError('not paired with ' + name)
    tried, last = [], None
    for address in candidates(name, row):
        if address in tried:
            continue
        tried.append(address)
        try:
            reply = request_at(address, row['cert'], request, timeout)
        except ssl.SSLCertVerificationError:
            raise ValueError('%s at %s answered with another identity; if it was reinstalled, pair again' % (name, address))
        except ssl.SSLError as error:
            if 'UNKNOWN_CA' in str(error) or 'CERTIFICATE' in str(error).upper():
                raise ValueError('%s does not know this machine (it forgot the pairing?); run peer pair on both' % name)
            last = error
            continue
        except (OSError, ssl.SSLError) as error:
            last = error
            continue
        if address != row.get('address'):
            remember(name, address=address)
        if 'error' in reply:
            raise ValueError(name + ': ' + reply['error'])
        return reply['result']
    raise ValueError('%s is not reachable (%s; tried %s). Is `peer serve` running there, on this network?'
                     % (name, last or 'not found on this network', ', '.join(tried) or 'nothing'))


def candidates(name, row):
    if row.get('address'):
        yield row['address']
    if not row.get('fixed'):
        found = browse(SERVICE, 2, only=name).get(name)
        if found:
            yield found


def request_at(address, cert, request, timeout):
    where, port = address.rsplit(':', 1)
    with socket.create_connection((where, int(port)), timeout=3) as raw:
        with client_context(cert).wrap_socket(raw) as tls:
            tls.settimeout(timeout)
            stream = tls.makefile('rwb')
            send(stream, request)
            return receive(stream)


# --- discovery (the system's own mDNS tools) -----------------------------------------------------------

def mdns():
    return not os.environ.get('PEER_LAN_NO_MDNS')


def advertise(name, service, port):
    if not mdns():
        return None
    if shutil.which('dns-sd'):
        command = ['dns-sd', '-R', name, service, 'local', str(port)]
    elif shutil.which('avahi-publish'):
        command = ['avahi-publish', '-s', name, service, str(port)]
    else:
        return None
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(process):
    if process:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


def output_for(command, seconds):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True)
    try:
        return process.communicate(timeout=seconds)[0]
    except subprocess.TimeoutExpired:
        process.terminate()
        return process.communicate()[0]


def first_match(command, pattern, seconds):
    """The first output line matching pattern, stopping the command right there (dns-sd never exits)."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True)
    timer = threading.Timer(seconds, process.terminate)
    timer.start()
    try:
        for line in process.stdout:
            match = re.search(pattern, line)
            if match:
                return match
        return None
    finally:
        timer.cancel()
        stop(process)


def browse(service, seconds=3, only=None):
    """{instance name: address:port} advertised on this network."""
    if not mdns():
        return {}
    found = {}
    if shutil.which('avahi-browse'):
        for line in output_for(['avahi-browse', '-rpt', service], seconds + 5).splitlines():
            fields = line.split(';')
            if len(fields) > 8 and fields[0] == '=' and fields[2] == 'IPv4':
                name = re.sub(r'\\(\d{3})', lambda m: chr(int(m.group(1))), fields[3])
                if fields[7].startswith('127.') and name in found:
                    continue
                found[name] = fields[7] + ':' + fields[8]
        return found
    if shutil.which('dns-sd'):
        names = [only] if only else []
        if not only:
            for line in output_for(['dns-sd', '-B', service, 'local'], seconds).splitlines():
                parts = line.split(None, 6)
                if len(parts) == 7 and parts[1] == 'Add':
                    names.append(parts[6].strip())
        for name in dict.fromkeys(names):
            match = first_match(['dns-sd', '-L', name, service, 'local'], r'can be reached at (\S+?)\.?:(\d+)', 2)
            if match:
                try:
                    address = socket.getaddrinfo(match.group(1), int(match.group(2)), socket.AF_INET)[0][4][0]
                except OSError:
                    continue
                found[name] = '%s:%s' % (address, match.group(2))
    return found


def local_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(('10.255.255.255', 1))
            return probe.getsockname()[0]
        except OSError:
            return '127.0.0.1'
