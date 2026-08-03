"""
House Bell — a LAN-only doorbell app.

Every device running this app is simultaneously a "sender" (can ring the
bell) and a "receiver" (hears rings from anyone else) — there's no
fixed door/receiver split, and no central server. Devices discover each
other via a lightweight gossip protocol over UDP, then use direct
unicast (reliable, ACKed at the WiFi radio level) to deliver rings —
with a UDP broadcast as the first attempt, since it's cheap and usually
works, and unicast as the fallback for whoever it didn't reach.

Protocol summary (all UDP, one socket, one port, messages dispatched by
a "type" field):

  ANNOUNCE        broadcast, periodic   "I exist, here's who I know about"
  ANNOUNCE_REPLY  unicast, on demand    "Here's who I know that you didn't
                                          mention" (delta only, not the
                                          whole list)
  PING            unicast, periodic     liveness check to each known peer
  ACK             unicast, reply        "still here"
  RING            broadcast, then       the actual doorbell event
                  unicast to gaps
  RING_ACK        unicast, reply        "got it, don't resend to me"

Peers are identified by a random session ID (generated fresh each time
the app starts), not by IP — so a phone reconnecting to WiFi and
getting a new IP doesn't look like a brand-new device.

We never need to self-report our own IP anywhere in a message: whoever
directly receives a packet from us (broadcast or unicast) already sees
our true IP as the packet's source address. The only place an IP
appears in a message is when relaying *someone else's* address as part
of gossip.
"""

import flet as ft
import flet_audio as fta
import socket
import threading
import json
import time
import uuid

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
PORT = 50000
BROADCAST_ADDR = "255.255.255.255"  # if your router blocks this, try your
                                     # subnet's directed broadcast instead,
                                     # e.g. 192.168.1.255
MAGIC = "HOUSE_BELL_V2"

ANNOUNCE_INTERVAL = 20      # seconds between periodic presence broadcasts
LIVENESS_INTERVAL = 15      # seconds between liveness ping rounds
PING_GRACE_WINDOW = 2.0     # seconds to wait for ACKs after a ping round
MAX_MISSED_PINGS = 3        # consecutive misses before a peer is evicted
RING_ACK_WAIT = 0.4         # seconds to wait for ring acks before filling
                             # gaps with direct unicast
MAX_PEERS_IN_PAYLOAD = 15   # hard cap — guarantees announce messages never
                             # need IP fragmentation
SEEN_RING_IDS_MAX = 50      # how many recent ring ids we remember, to
                             # avoid re-alerting on a duplicate delivery

SESSION_ID = uuid.uuid4().hex  # fresh identity every time the app starts


def main(page: ft.Page):
    page.title = "House Bell"
    page.window_width = 400
    page.window_height = 700
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20
    page.vertical_alignment = ft.MainAxisAlignment.CENTER
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER

    # -------------------------------------------------------------
    # Shared state
    # -------------------------------------------------------------
    listening = {"active": True}

    # peers: session_id -> {"ip": str, "last_seen": float, "missed_pings": int}
    # Only OTHER devices go in here — never ourselves.
    peers = {}
    peers_lock = threading.Lock()

    # ring_id -> set of session_ids who have ack'd this specific ring.
    # Short-lived: created in send_ring(), consumed a few hundred ms later.
    pending_ring_acks = {}
    pending_lock = threading.Lock()

    # Recently handled ring ids, so a duplicate delivery (e.g. our ack
    # got lost and the sender unicast-retried to us) doesn't re-trigger
    # the alert a second time.
    seen_ring_ids = []

    status_text = ft.Text("", size=14, color=ft.Colors.GREY_600)
    peer_count_text = ft.Text("0 known device(s)", size=12, color=ft.Colors.GREY_500)

    def listening_indicator():
        return ft.Row(
            [
                ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.GREEN_400, size=10),
                ft.Text("Listening for rings...", size=12, color=ft.Colors.GREY_600),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
        )

    alert_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("🔔 Someone is at the door!"),
        content=ft.Text(""),
        actions=[],
    )

    # Bundle a bell.mp3 inside an "assets" folder next to this file. Audio
    # is a Service in this Flet version, not a visual control — it's
    # auto-registered just by being created, and must NOT be added to
    # page.overlay (that causes an "Unknown control: Audio" error).
    audio = fta.Audio(src="bell.mp3", autoplay=False, volume=1.0)

    def update_peer_count():
        with peers_lock:
            n = len(peers)
        peer_count_text.value = f"{n} known device(s)"
        page.update()

    # -------------------------------------------------------------
    # Peer list helpers (all peers_lock-protected)
    # -------------------------------------------------------------

    def snapshot_known_peers():
        """Our current peer list, trimmed to the most-recently-seen N,
        formatted for sending in an ANNOUNCE. Guarantees a bounded,
        single-packet-sized payload regardless of how many peers we
        eventually accumulate."""
        with peers_lock:
            items = sorted(peers.items(), key=lambda kv: kv[1]["last_seen"], reverse=True)
            items = items[:MAX_PEERS_IN_PAYLOAD]
            return {sid: {"ip": e["ip"], "last_seen": e["last_seen"]} for sid, e in items}

    def merge_peer_list(incoming):
        """Merge a dict of {session_id: {ip, last_seen}} into our peers.
        'Newer last_seen wins' is the whole rule — this is what lets one
        lucky exchange propagate knowledge through the whole group."""
        if not incoming:
            return
        changed = False
        with peers_lock:
            for sid, entry in incoming.items():
                if sid == SESSION_ID:
                    continue  # never store ourselves
                existing = peers.get(sid)
                incoming_last_seen = entry.get("last_seen", 0)
                if existing is None or incoming_last_seen > existing["last_seen"]:
                    peers[sid] = {
                        "ip": entry.get("ip"),
                        "last_seen": incoming_last_seen,
                        "missed_pings": 0,
                    }
                    changed = True
        if changed:
            update_peer_count()

    def compute_diff(their_known_peers):
        """Entries WE have that are unknown to them, or newer than what
        they reported having. Used so replies only carry the delta,
        not the whole list every time."""
        their_known_peers = their_known_peers or {}
        with peers_lock:
            diff = {}
            for sid, entry in peers.items():
                theirs = their_known_peers.get(sid)
                if theirs is None or entry["last_seen"] > theirs.get("last_seen", 0):
                    diff[sid] = {"ip": entry["ip"], "last_seen": entry["last_seen"]}
            return diff

    # -------------------------------------------------------------
    # Low-level send helpers
    # -------------------------------------------------------------

    def make_socket():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        return s

    def send_msg(sock, msg_type, target_ip, payload=None):
        msg = {"magic": MAGIC, "type": msg_type, "session": SESSION_ID, "payload": payload or {}}
        try:
            sock.sendto(json.dumps(msg).encode(), (target_ip, PORT))
        except Exception:
            pass  # best-effort — a single failed send isn't fatal anywhere it's used

    # -------------------------------------------------------------
    # Background loop 1: periodic ANNOUNCE (broadcast)
    # -------------------------------------------------------------

    def announce_loop():
        sock = make_socket()
        while listening["active"]:
            send_msg(sock, "ANNOUNCE", BROADCAST_ADDR, {"known_peers": snapshot_known_peers()})
            time.sleep(ANNOUNCE_INTERVAL)
        sock.close()

    # -------------------------------------------------------------
    # Background loop 2: periodic liveness PING to known peers
    # -------------------------------------------------------------

    def liveness_loop():
        sock = make_socket()
        while listening["active"]:
            time.sleep(LIVENESS_INTERVAL)
            with peers_lock:
                targets = list(peers.items())
            for sid, entry in targets:
                send_msg(sock, "PING", entry["ip"])

            time.sleep(PING_GRACE_WINDOW)

            with peers_lock:
                stale = []
                for sid, entry in peers.items():
                    if entry.pop("acked_this_round", False):
                        entry["missed_pings"] = 0
                    else:
                        entry["missed_pings"] = entry.get("missed_pings", 0) + 1
                        if entry["missed_pings"] >= MAX_MISSED_PINGS:
                            stale.append(sid)
                for sid in stale:
                    del peers[sid]
            update_peer_count()
        sock.close()

    # -------------------------------------------------------------
    # Main receive loop — one socket, dispatch by message type
    # -------------------------------------------------------------

    def listen_loop():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", PORT))
        except Exception as ex:
            status_text.value = f"Could not bind port: {ex}"
            page.update()
            return
        sock.settimeout(1.0)

        reply_sock = make_socket()  # separate socket for unicast replies/sends

        while listening["active"]:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception:
                continue

            sender_ip = addr[0]
            try:
                msg = json.loads(data.decode())
            except Exception:
                continue
            if msg.get("magic") != MAGIC:
                continue
            sender_sid = msg.get("session")
            if sender_sid == SESSION_ID:
                continue  # ignore our own broadcast reaching ourselves
            msg_type = msg.get("type")
            payload = msg.get("payload") or {}

            # Any direct message at all is proof this peer is really
            # reachable at this address right now — record it.
            merge_peer_list({sender_sid: {"ip": sender_ip, "last_seen": time.time()}})

            if msg_type == "ANNOUNCE":
                incoming_known = payload.get("known_peers", {})
                merge_peer_list(incoming_known)
                diff = compute_diff(incoming_known)
                if diff:
                    send_msg(reply_sock, "ANNOUNCE_REPLY", sender_ip, {"known_peers": diff})

            elif msg_type == "ANNOUNCE_REPLY":
                merge_peer_list(payload.get("known_peers", {}))

            elif msg_type == "PING":
                send_msg(reply_sock, "ACK", sender_ip)

            elif msg_type == "ACK":
                with peers_lock:
                    if sender_sid in peers:
                        peers[sender_sid]["acked_this_round"] = True
                        peers[sender_sid]["ip"] = sender_ip
                        peers[sender_sid]["last_seen"] = time.time()

            elif msg_type == "RING":
                ring_id = payload.get("ring_id")
                if ring_id and ring_id not in seen_ring_ids:
                    seen_ring_ids.append(ring_id)
                    del seen_ring_ids[:-SEEN_RING_IDS_MAX]  # keep bounded
                    page.run_task(trigger_alert, sender_ip)
                # ack even on a duplicate — idempotent, and tells the
                # sender to stop retrying to us regardless
                if ring_id:
                    send_msg(reply_sock, "RING_ACK", sender_ip, {"ring_id": ring_id})

            elif msg_type == "RING_ACK":
                ring_id = payload.get("ring_id")
                with pending_lock:
                    if ring_id in pending_ring_acks:
                        pending_ring_acks[ring_id].add(sender_sid)

        sock.close()
        reply_sock.close()

    # -------------------------------------------------------------
    # Alert handling
    # -------------------------------------------------------------

    def dismiss_alert(e):
        page.pop_dialog()

    async def trigger_alert(sender_ip):
        alert_dialog.content = ft.Text(f"From {sender_ip}", size=16)
        alert_dialog.actions = [ft.TextButton("Dismiss", on_click=dismiss_alert)]
        try:
            await audio.play()
        except Exception as ex:
            status_text.value = f"Audio error: {ex}"
            page.update()
        try:
            page.show_dialog(alert_dialog)
        except RuntimeError:
            # a dialog is already open (e.g. rings arrived close together)
            page.update()

    # -------------------------------------------------------------
    # Ring delivery: broadcast first, fill gaps with direct unicast
    # -------------------------------------------------------------

    def send_ring(e):
        threading.Thread(target=_send_ring_worker, daemon=True).start()

    def _send_ring_worker():
        ring_id = uuid.uuid4().hex
        with pending_lock:
            pending_ring_acks[ring_id] = set()

        sock = make_socket()
        try:
            # 1. Broadcast — cheap, usually works, and reaches devices we
            #    haven't discovered yet (new app installs, just opened).
            send_msg(sock, "RING", BROADCAST_ADDR, {"ring_id": ring_id})
            status_text.value = "Ring sent — confirming delivery..."
            page.update()

            # 2. Give acks a short window to arrive.
            time.sleep(RING_ACK_WAIT)

            # 3. Fill the gap: unicast directly to any KNOWN peer who
            #    didn't ack. Unknown/undiscovered devices already got
            #    their one shot via the broadcast above.
            with pending_lock:
                acked = pending_ring_acks.pop(ring_id, set())
            with peers_lock:
                known_peers = dict(peers)
            missing = set(known_peers.keys()) - acked

            for sid in missing:
                send_msg(sock, "RING", known_peers[sid]["ip"], {"ring_id": ring_id})

            if missing:
                status_text.value = f"Ring sent ✅ (resent directly to {len(missing)} device(s))"
            else:
                status_text.value = "Ring sent ✅ (confirmed by all known devices)"
        except Exception as ex:
            status_text.value = f"Error sending: {ex}"
        finally:
            sock.close()
        page.update()

    # -------------------------------------------------------------
    # UI — a single screen
    # -------------------------------------------------------------

    def show_main_screen():
        page.controls.clear()
        page.add(
            ft.Text("🏠 House Bell", size=32, weight=ft.FontWeight.BOLD),
            ft.Container(height=40),
            ft.Button(
                content="🔔  RING",
                on_click=send_ring,
                height=140, width=140,
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.RED_500, color=ft.Colors.WHITE,
                    shape=ft.RoundedRectangleBorder(radius=70),
                    text_style=ft.TextStyle(size=20, weight=ft.FontWeight.BOLD),
                ),
            ),
            ft.Container(height=30),
            status_text,
            listening_indicator(),
            peer_count_text,
        )
        page.update()

    # -------------------------------------------------------------
    # Startup — kick off all three background loops, then show the UI
    # -------------------------------------------------------------
    threading.Thread(target=listen_loop, daemon=True).start()
    threading.Thread(target=announce_loop, daemon=True).start()
    threading.Thread(target=liveness_loop, daemon=True).start()

    show_main_screen()


ft.run(main, assets_dir="assets")
