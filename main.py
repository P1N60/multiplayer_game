import argparse
import ipaddress
import json
import os
import random
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from types import SimpleNamespace
from typing import Dict, Optional, Tuple

import pygame


SCREEN_WIDTH = 960
SCREEN_HEIGHT = 640
WINDOWED_WIDTH = 1280
WINDOWED_HEIGHT = 720
MAP_WIDTH = 3200
MAP_HEIGHT = 2000
PLAYER_RADIUS = 14
PLAYER_SPEED = 230.0
TICK_RATE = 60
STATE_BROADCAST_HZ = 20
NETWORK_TIMEOUT_SECONDS = 8.0
REMOTE_SMOOTHING = 0.1
RESPAWN_SECONDS = 3.0
MENU_BG = (17, 19, 24)
MENU_PANEL = (28, 31, 38)
MENU_TEXT = (235, 235, 235)
MENU_MUTED = (165, 170, 185)
MENU_ACCENT = (94, 200, 255)


def parse_port_or_default(value: str, default: int = 5000) -> int:
    try:
        port = int(value.strip())
    except ValueError:
        return default
    return port if 1 <= port <= 65535 else default


def is_global_ipv4(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_global


def build_network_help(mode: str, host_value: str, port_value: str, lan_ip: str, public_ip: str):
    port = parse_port_or_default(port_value)
    items = []

    if mode == "host":
        items.append((f"Host bind: 0.0.0.0:{port} (all local interfaces)", MENU_TEXT))
        items.append((f"Share this with friend: {public_ip}:{port}", MENU_ACCENT))

        if not is_global_ipv4(public_ip):
            items.append(("Public IP unavailable/non-global. Internet hosting may fail (possible CGNAT).", (255, 150, 150)))
        else:
            items.append(("Router: forward UDP port to this PC's LAN IP.", MENU_MUTED))

        items.append((f"Forward UDP {port} -> {lan_ip}:{port} in router settings.", MENU_MUTED))
        items.append(("Allow inbound UDP on Windows Firewall for this app/port.", MENU_MUTED))
    else:
        host = host_value.strip() or "127.0.0.1"
        items.append((f"Client target: {host}:{port}", MENU_TEXT))
        if host in {"127.0.0.1", "localhost"}:
            items.append(("127.0.0.1 only works on your own PC.", (255, 150, 150)))
        else:
            items.append(("Use host's public IP for internet, LAN IP for same network.", MENU_MUTED))

    return items


def configure_windows_firewall_udp(port: int) -> Tuple[bool, str]:
    if os.name != "nt":
        return False, "Auto firewall setup is Windows-only."

    in_rule = f"MultiplayerTopDown UDP {port} In"
    out_rule = f"MultiplayerTopDown UDP {port} Out"

    def run_netsh(args):
        return subprocess.run(args, capture_output=True, text=True, check=False)

    try:
        run_netsh(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={in_rule}"])
        run_netsh(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={out_rule}"])

        in_result = run_netsh(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={in_rule}",
                "dir=in",
                "action=allow",
                "protocol=UDP",
                f"localport={port}",
            ]
        )
        out_result = run_netsh(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={out_rule}",
                "dir=out",
                "action=allow",
                "protocol=UDP",
                f"localport={port}",
            ]
        )

        combined = "\n".join([in_result.stdout, in_result.stderr, out_result.stdout, out_result.stderr]).lower()
        if in_result.returncode != 0 or out_result.returncode != 0:
            if "access is denied" in combined:
                return False, "Firewall setup needs admin rights. Run the game as administrator once."
            return False, "Firewall rule setup failed. Open UDP port manually in Windows Firewall."

        return True, f"Windows Firewall updated: UDP {port} inbound/outbound allowed."
    except FileNotFoundError:
        return False, "netsh not found. Configure firewall manually."


@dataclass
class PlayerState:
    player_id: str
    name: str
    x: float
    y: float
    color: Tuple[int, int, int]
    last_seen: float
    respawn_at: float = 0.0


class NetSession:
    def __init__(
        self,
        mode: str,
        bind_host: str,
        host: str,
        port: int,
        local_id: str,
        local_name: str,
        world_width: int,
        world_height: int,
    ):
        self.mode = mode
        self.host_addr = (host, port)
        self.local_id = local_id
        self.local_name = local_name
        self.world_width = world_width
        self.world_height = world_height

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.settimeout(0.05)
        self.socket.bind((bind_host, port if mode == "host" else 0))

        self.running = False
        self.lock = threading.Lock()

        self.host_client_addrs = set()
        self.players: Dict[str, PlayerState] = {}

    def start(self):
        self.running = True
        threading.Thread(target=self._network_loop, daemon=True).start()

    def stop(self):
        self.running = False
        self.socket.close()

    def send_hello(self):
        payload = {"type": "hello", "id": self.local_id, "name": self.local_name}
        self._send_json(payload, self.host_addr)

    def send_local_state(self, x: float, y: float, color: Tuple[int, int, int]):
        payload = {
            "type": "state",
            "id": self.local_id,
            "name": self.local_name,
            "x": x,
            "y": y,
            "color": color,
            "t": time.time(),
        }

        if self.mode == "host":
            self._upsert_player(payload)
        else:
            self._send_json(payload, self.host_addr)

    def get_players_snapshot(self) -> Dict[str, PlayerState]:
        now = time.time()
        with self.lock:
            stale = [pid for pid, p in self.players.items() if now - p.last_seen > NETWORK_TIMEOUT_SECONDS]
            for pid in stale:
                del self.players[pid]
            return dict(self.players)

    def _network_loop(self):
        if self.mode == "client":
            self.send_hello()

        last_broadcast = 0.0
        while self.running:
            try:
                raw, addr = self.socket.recvfrom(65535)
                packet = json.loads(raw.decode("utf-8"))
                self._handle_packet(packet, addr)
            except socket.timeout:
                pass
            except OSError:
                break
            except json.JSONDecodeError:
                continue

            if self.mode == "host":
                now = time.time()
                self._tick_host_rules(now)
                if now - last_broadcast >= 1.0 / STATE_BROADCAST_HZ:
                    self._broadcast_world_state()
                    last_broadcast = now

    def _handle_packet(self, packet: dict, addr):
        p_type = packet.get("type")

        if self.mode == "host":
            if p_type in {"hello", "state"}:
                self.host_client_addrs.add(addr)
                if p_type == "state":
                    self._upsert_player(packet)
                elif p_type == "hello":
                    with self.lock:
                        if packet.get("id") not in self.players:
                            self.players[packet["id"]] = PlayerState(
                                player_id=packet["id"],
                                name=packet.get("name", "Guest"),
                                x=random.uniform(50, self.world_width - 50),
                                y=random.uniform(50, self.world_height - 50),
                                color=self._random_color(),
                                last_seen=time.time(),
                            )

        if self.mode == "client" and p_type == "world":
            now = time.time()
            incoming = packet.get("players", [])
            with self.lock:
                for p in incoming:
                    self.players[p["player_id"]] = PlayerState(
                        player_id=p["player_id"],
                        name=p["name"],
                        x=float(p["x"]),
                        y=float(p["y"]),
                        color=tuple(p["color"]),
                        last_seen=now,
                        respawn_at=float(p.get("respawn_at", 0.0)),
                    )

    def _broadcast_world_state(self):
        with self.lock:
            payload = {
                "type": "world",
                "players": [asdict(p) for p in self.players.values()],
            }

        for addr in list(self.host_client_addrs):
            self._send_json(payload, addr)

    def _upsert_player(self, packet: dict):
        now = time.time()
        with self.lock:
            pid = packet["id"]
            existing = self.players.get(pid)

            is_dead = bool(existing and existing.respawn_at > now)
            next_x = float(packet.get("x", existing.x if existing else self.world_width / 2))
            next_y = float(packet.get("y", existing.y if existing else self.world_height / 2))
            if is_dead and existing:
                next_x = existing.x
                next_y = existing.y

            self.players[pid] = PlayerState(
                player_id=pid,
                name=packet.get("name", existing.name if existing else "Guest"),
                x=next_x,
                y=next_y,
                color=tuple(packet.get("color", existing.color if existing else self._random_color())),
                last_seen=now,
                respawn_at=existing.respawn_at if existing else 0.0,
            )

    def _tick_host_rules(self, now: float):
        with self.lock:
            host_player = self.players.get(self.local_id)
            if host_player is None:
                return

            # Resolve pending respawns first so dead players come back after the timer.
            for pid, player in list(self.players.items()):
                if player.respawn_at > 0.0 and now >= player.respawn_at:
                    player.x = random.uniform(50, self.world_width - 50)
                    player.y = random.uniform(50, self.world_height - 50)
                    player.respawn_at = 0.0

            host_x, host_y = host_player.x, host_player.y
            touch_distance_sq = (PLAYER_RADIUS * 2) ** 2

            for pid, player in self.players.items():
                if pid == self.local_id:
                    continue
                if player.respawn_at > now:
                    continue

                dx = player.x - host_x
                dy = player.y - host_y
                if (dx * dx + dy * dy) <= touch_distance_sq:
                    player.respawn_at = now + RESPAWN_SECONDS
                    # Keep dead players off-screen until respawn.
                    player.x = -1000.0
                    player.y = -1000.0

    def _send_json(self, payload: dict, addr):
        try:
            data = json.dumps(payload).encode("utf-8")
            self.socket.sendto(data, addr)
        except OSError:
            pass

    @staticmethod
    def _random_color() -> Tuple[int, int, int]:
        return (random.randint(70, 255), random.randint(70, 255), random.randint(70, 255))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_args():
    parser = argparse.ArgumentParser(description="Basic Python peer-hosted multiplayer top-down game")
    parser.add_argument("--mode", choices=["host", "client"], help="Run as host or client")
    parser.add_argument("--host", default="127.0.0.1", help="Host IP for clients to connect to")
    parser.add_argument("--bind-host", default="0.0.0.0", help="Interface to bind when hosting")
    parser.add_argument("--port", type=int, default=5000, help="UDP port")
    parser.add_argument("--name", default="Player", help="Display name")
    return parser.parse_args()


def _draw_text(surface, font, text: str, pos: Tuple[int, int], color=MENU_TEXT):
    rendered = font.render(text, True, color)
    surface.blit(rendered, pos)


def detect_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "Unknown"


def detect_public_ip(timeout_seconds: float = 1.2) -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout_seconds) as response:
            return response.read().decode("utf-8").strip() or "Unknown"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "Unknown"


def launch_menu() -> Optional[SimpleNamespace]:
    pygame.init()
    pygame.display.set_caption("Multiplayer Top-Down - Launcher")
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
    clock = pygame.time.Clock()

    title_font = pygame.font.SysFont("consolas", 42)
    body_font = pygame.font.SysFont("consolas", 24)
    small_font = pygame.font.SysFont("consolas", 20)
    tiny_font = pygame.font.SysFont("consolas", 18)

    lan_ip = detect_lan_ip()
    public_ip = detect_public_ip()

    mode = "client"
    fields = {
        "name": f"Player{random.randint(100, 999)}",
        "host": "127.0.0.1",
        "port": "5000",
    }
    field_order = ["name", "host", "port"]
    labels = {
        "name": "Name",
        "host": "Host IP",
        "port": "Port",
    }
    active_idx = 0
    show_network_help = True
    status_message = ""
    status_until = 0.0

    running = True
    while running:
        dt = clock.tick(TICK_RATE)
        _ = dt

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                return None

            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    pygame.quit()
                    return None

                if event.key == pygame.K_F1:
                    mode = "host"
                elif event.key == pygame.K_F2:
                    mode = "client"
                elif event.key == pygame.K_F3:
                    show_network_help = not show_network_help
                elif event.key == pygame.K_F5:
                    port = parse_port_or_default(fields["port"])
                    ok, message = configure_windows_firewall_udp(port)
                    status_message = message
                    status_until = time.time() + 5.0
                elif event.key in (pygame.K_TAB, pygame.K_DOWN):
                    active_idx = (active_idx + 1) % len(field_order)
                elif event.key == pygame.K_UP:
                    active_idx = (active_idx - 1) % len(field_order)
                elif event.key == pygame.K_BACKSPACE:
                    active = field_order[active_idx]
                    fields[active] = fields[active][:-1]
                elif event.key == pygame.K_RETURN:
                    name = fields["name"].strip() or "Player"
                    host = (fields["host"].strip() or "127.0.0.1") if mode == "client" else "0.0.0.0"
                    bind_host = "0.0.0.0"
                    port = parse_port_or_default(fields["port"])

                    pygame.quit()
                    return SimpleNamespace(mode=mode, name=name, host=host, bind_host=bind_host, port=port)
                else:
                    ch = event.unicode
                    active = field_order[active_idx]

                    if active == "port":
                        if ch.isdigit() and len(fields[active]) < 5:
                            fields[active] += ch
                    else:
                        if ch.isprintable() and ch not in "\t\r\n" and len(fields[active]) < 24:
                            fields[active] += ch

        screen.fill(MENU_BG)
        panel_rect = pygame.Rect(140, 90, SCREEN_WIDTH - 280, SCREEN_HEIGHT - 180)
        pygame.draw.rect(screen, MENU_PANEL, panel_rect, border_radius=14)

        _draw_text(screen, title_font, "MULTIPLAYER TOP-DOWN", (190, 130), MENU_TEXT)
        _draw_text(screen, body_font, "F1: Host Lobby   F2: Join Lobby", (220, 188), MENU_MUTED)

        mode_label = "HOST" if mode == "host" else "JOIN"
        _draw_text(screen, body_font, f"Selected Mode: {mode_label}", (240, 236), MENU_ACCENT)

        start_y = 300
        for i, key in enumerate(field_order):
            y = start_y + i * 64
            label_color = MENU_ACCENT if i == active_idx else MENU_MUTED
            value_color = MENU_TEXT if i == active_idx else (210, 210, 210)

            _draw_text(screen, body_font, f"{labels[key]}:", (220, y), label_color)
            pygame.draw.rect(screen, (40, 44, 56), pygame.Rect(390, y - 6, 330, 38), border_radius=8)
            _draw_text(screen, body_font, fields[key] or " ", (402, y), value_color)

        if mode == "client" and fields["host"].strip() in {"127.0.0.1", "localhost"}:
            _draw_text(
                screen,
                tiny_font,
                "Warning: 127.0.0.1 only connects to your own PC.",
                (220, 498),
                (255, 140, 140),
            )

        _draw_text(screen, tiny_font, f"Your LAN IP: {lan_ip}  |  Public IP: {public_ip}", (190, 530), MENU_MUTED)
        _draw_text(screen, small_font, "Arrows/Tab: Select field   Enter: Start   Esc: Quit", (190, 550), MENU_MUTED)
        _draw_text(screen, small_font, "F3: Toggle network help   F5: Auto firewall rule (Windows)", (190, 575), MENU_MUTED)

        if show_network_help:
            help_lines = build_network_help(mode, fields["host"], fields["port"], lan_ip, public_ip)
            help_y = 600
            for text, color in help_lines[:2]:
                _draw_text(screen, tiny_font, text, (190, help_y), color)
                help_y += 22

        if status_message and time.time() < status_until:
            status_color = (140, 240, 170) if "updated" in status_message.lower() else (255, 170, 170)
            _draw_text(screen, tiny_font, status_message, (190, 620), status_color)

        pygame.display.flip()

    pygame.quit()
    return None


def run_game(args):
    pygame.init()
    pygame.display.set_caption("Multiplayer Top-Down Prototype")
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    is_fullscreen = True

    def apply_display_mode(fullscreen_enabled: bool):
        nonlocal screen
        if fullscreen_enabled:
            screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            screen = pygame.display.set_mode((WINDOWED_WIDTH, WINDOWED_HEIGHT))

    view_width, view_height = screen.get_size()
    world_width, world_height = MAP_WIDTH, MAP_HEIGHT
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("consolas", 20)

    local_id = f"{args.name}-{random.randint(1000, 9999)}"
    local_color = (80, 220, 120) if args.mode == "host" else (80, 180, 255)
    local_x = random.uniform(80, world_width - 80)
    local_y = random.uniform(80, world_height - 80)
    render_positions: Dict[str, Tuple[float, float]] = {}

    session = NetSession(
        mode=args.mode,
        bind_host=args.bind_host,
        host=args.host,
        port=args.port,
        local_id=local_id,
        local_name=args.name,
        world_width=world_width,
        world_height=world_height,
    )
    session.start()

    running = True
    while running:
        dt = clock.tick(TICK_RATE) / 1000.0
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
            elif event.type == pygame.KEYDOWN:
                alt_enter = event.key == pygame.K_RETURN and (event.mod & pygame.KMOD_ALT)
                f11 = event.key == pygame.K_F11
                if alt_enter or f11:
                    is_fullscreen = not is_fullscreen
                    apply_display_mode(is_fullscreen)
                    view_width, view_height = screen.get_size()
                    local_x = clamp(local_x, PLAYER_RADIUS, world_width - PLAYER_RADIUS)
                    local_y = clamp(local_y, PLAYER_RADIUS, world_height - PLAYER_RADIUS)

        net_players = session.get_players_snapshot()
        local_net_state = net_players.get(local_id)
        local_dead = bool(local_net_state and local_net_state.respawn_at > time.time())

        keys = pygame.key.get_pressed()
        move_x = float(keys[pygame.K_d] or keys[pygame.K_RIGHT]) - float(keys[pygame.K_a] or keys[pygame.K_LEFT])
        move_y = float(keys[pygame.K_s] or keys[pygame.K_DOWN]) - float(keys[pygame.K_w] or keys[pygame.K_UP])

        if not local_dead:
            local_x = clamp(local_x + move_x * PLAYER_SPEED * dt, PLAYER_RADIUS, world_width - PLAYER_RADIUS)
            local_y = clamp(local_y + move_y * PLAYER_SPEED * dt, PLAYER_RADIUS, world_height - PLAYER_RADIUS)
        elif local_net_state:
            local_x = local_net_state.x
            local_y = local_net_state.y

        session.send_local_state(local_x, local_y, local_color)

        players = dict(net_players)
        if local_dead and local_net_state:
            players[local_id] = local_net_state
        else:
            # Always render local player from current input position to avoid self-induced network lag.
            players[local_id] = PlayerState(local_id, args.name, local_x, local_y, local_color, time.time())

        active_ids = set(players.keys())
        for stale_id in list(render_positions.keys()):
            if stale_id not in active_ids:
                del render_positions[stale_id]

        # Smooth only remote players so network jitter is less visible.
        for pid, p in players.items():
            prev_x, prev_y = render_positions.get(pid, (p.x, p.y))
            if pid == local_id:
                render_positions[pid] = (p.x, p.y)
            else:
                smooth_x = prev_x + (p.x - prev_x) * REMOTE_SMOOTHING
                smooth_y = prev_y + (p.y - prev_y) * REMOTE_SMOOTHING
                render_positions[pid] = (smooth_x, smooth_y)

        now = time.time()
        camera_x = clamp(local_x - (view_width / 2), 0, world_width - view_width)
        camera_y = clamp(local_y - (view_height / 2), 0, world_height - view_height)

        screen.fill((22, 24, 30))
        # Draw a simple world grid so camera movement is easy to perceive.
        grid_color = (34, 38, 48)
        grid_step = 120
        start_x = int(camera_x // grid_step) * grid_step
        start_y = int(camera_y // grid_step) * grid_step
        end_x = int(camera_x + view_width) + grid_step
        end_y = int(camera_y + view_height) + grid_step
        for gx in range(start_x, end_x, grid_step):
            sx = int(gx - camera_x)
            pygame.draw.line(screen, grid_color, (sx, 0), (sx, view_height), 1)
        for gy in range(start_y, end_y, grid_step):
            sy = int(gy - camera_y)
            pygame.draw.line(screen, grid_color, (0, sy), (view_width, sy), 1)

        for pid, p in players.items():
            if p.respawn_at > now:
                continue

            draw_x, draw_y = render_positions.get(pid, (p.x, p.y))
            screen_x = int(draw_x - camera_x)
            screen_y = int(draw_y - camera_y)
            if screen_x < -PLAYER_RADIUS or screen_x > view_width + PLAYER_RADIUS:
                continue
            if screen_y < -PLAYER_RADIUS or screen_y > view_height + PLAYER_RADIUS:
                continue

            pygame.draw.circle(screen, p.color, (screen_x, screen_y), PLAYER_RADIUS)
            name_surface = font.render(p.name, True, (240, 240, 240))
            screen.blit(name_surface, (screen_x - name_surface.get_width() // 2, screen_y - 34))

        mode_text = f"Mode: {args.mode} | players: {len(players)} | map: {world_width}x{world_height}"
        hud = font.render(mode_text, True, (230, 230, 230))
        screen.blit(hud, (12, 10))

        if local_dead and local_net_state:
            remaining = max(0.0, local_net_state.respawn_at - now)
            death_text = font.render(f"You were tagged by host. Respawning in {remaining:.1f}s", True, (255, 160, 160))
            screen.blit(death_text, (12, 36))

        pygame.display.flip()

    session.stop()
    pygame.quit()


if __name__ == "__main__":
    start_args = parse_args()
    if start_args.mode is None:
        start_args = launch_menu()
    if start_args is not None:
        run_game(start_args)
