"""
WebSocket client for Picar — low-latency alternative to picar_client.py (REST).

Features:
    - Persistent WebSocket connection (no per-command HTTP overhead)
    - Auto-reconnect with exponential backoff on disconnect
    - Sensor push subscription (Pico streams data without polling)
    - Thread-safe sync wrapper for use in existing FSM/terminal code
    - Same API surface as PicarClient for easy migration

Usage (async):
    import asyncio
    from picar_ws_client import PicarWsClient

    async def main():
        client = PicarWsClient("192.168.178.59")
        await client.connect()
        await client.set_motor(50)
        await client.set_servo(120)
        sensors = await client.get_sensors()
        await client.disconnect()

    asyncio.run(main())

Usage (sync wrapper — drop-in replacement):
    from picar_ws_client import PicarWsClientSync

    client = PicarWsClientSync("192.168.178.59")
    client.connect()
    client.set_motor(50)
    client.set_servo(120)
    sensors = client.get_sensors()
    client.disconnect()

Usage (terminal keyboard control):
    python picar_ws_client.py
"""

import asyncio
import json
import time
import sys
import threading
from pathlib import Path
from typing import Optional, Callable, Dict, Any

try:
    import websockets
except ImportError:
    print("ERROR: websockets library required. Install with:")
    print("  pip install websockets")
    sys.exit(1)

# Import config for default IP
try:
    parent_dir = Path(__file__).parent.parent
    if str(parent_dir) not in sys.path:
        sys.path.insert(0, str(parent_dir))
    import config
    PICAR_IP = config.car_ip
except (ImportError, AttributeError):
    print("⚠️  Warning: Could not import config.py, using default IP")
    PICAR_IP = "192.168.178.59"


class PicarWsClient:
    """Async WebSocket client for Picar with auto-reconnect."""

    def __init__(self, ip: str = PICAR_IP, port: int = 5000):
        self.uri = f"ws://{ip}:{port}/ws"
        self.ip = ip
        self.port = port
        self._ws = None
        self._connected = False
        self._reconnecting = False
        self._should_run = False
        self._reconnect_task = None
        self._sensor_callback: Optional[Callable[[dict], None]] = None
        self._receive_task = None
        self._pending_responses: asyncio.Queue = None
        self._lock = asyncio.Lock()
        self.auto_lights = True
        self._last_light_target = ""

        # Connection stats
        self.reconnect_count = 0
        self.last_rtt_ms = 0

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    async def connect(self, timeout: float = 5.0) -> bool:
        """Connect to Pico WebSocket server."""
        self._should_run = True
        self._pending_responses = asyncio.Queue()
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.uri, ping_interval=5, ping_timeout=10),
                timeout=timeout
            )
            self._connected = True
            self._reconnecting = False
            print(f"✓ WebSocket connected to {self.uri}")

            # Start background receiver
            self._receive_task = asyncio.create_task(self._receiver_loop())
            return True

        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            print(f"✗ Connection failed: {e}")
            self._connected = False
            # Start reconnect loop
            if self._should_run:
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())
            return False

    async def disconnect(self):
        """Gracefully disconnect."""
        self._should_run = False
        self._connected = False

        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self._receive_task:
            self._receive_task.cancel()
            self._receive_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        print("✓ Disconnected")

    async def _send_command(self, cmd: dict, timeout: float = 3.0) -> dict:
        """Send command and wait for response."""
        if not self._connected or not self._ws:
            if self._reconnecting:
                return {"ok": 0, "e": "reconnecting"}
            return {"ok": 0, "e": "not connected"}

        async with self._lock:
            t0 = time.time()
            try:
                await self._ws.send(json.dumps(cmd))
                # Wait for response from receiver loop
                response = await asyncio.wait_for(
                    self._pending_responses.get(), timeout=timeout
                )
                self.last_rtt_ms = (time.time() - t0) * 1000
                return response
            except asyncio.TimeoutError:
                return {"ok": 0, "e": "timeout"}
            except (websockets.ConnectionClosed, OSError) as e:
                self._connected = False
                if self._should_run:
                    self._reconnect_task = asyncio.create_task(self._reconnect_loop())
                return {"ok": 0, "e": f"disconnected: {e}"}

    async def _receiver_loop(self):
        """Background task: receive messages and dispatch."""
        try:
            async for message in self._ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue

                # Sensor push messages (no "ok" key, have "t":"sns")
                if data.get("t") == "sns":
                    if self._sensor_callback:
                        self._sensor_callback(data)
                else:
                    # Command response
                    await self._pending_responses.put(data)

        except (websockets.ConnectionClosed, OSError) as e:
            print(f"⚠️  Connection lost: {e}")
        finally:
            self._connected = False
            if self._should_run:
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Reconnect with exponential backoff."""
        if self._reconnecting:
            return
        self._reconnecting = True

        backoff = 0.5
        max_backoff = 10.0

        while self._should_run and not self._connected:
            print(f"⟳ Reconnecting in {backoff:.1f}s...")
            await asyncio.sleep(backoff)

            if not self._should_run:
                break

            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(self.uri, ping_interval=5, ping_timeout=10),
                    timeout=5.0
                )
                self._connected = True
                self._reconnecting = False
                self.reconnect_count += 1
                self._pending_responses = asyncio.Queue()
                self._receive_task = asyncio.create_task(self._receiver_loop())
                print(f"✓ Reconnected (attempt #{self.reconnect_count})")
                return

            except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
                backoff = min(backoff * 2, max_backoff)

        self._reconnecting = False

    # ========== Motor Control ==========

    async def set_motor(self, speed: int) -> dict:
        """Set motor speed (-100 to 100)."""
        speed = max(-100, min(100, int(speed)))
        result = await self._send_command({"c": "m", "v": speed})

        # Auto lights (non-blocking)
        if self.auto_lights:
            if speed > 0:
                target = "front"
            elif speed < 0:
                target = "back"
            else:
                target = "off"
            if target != self._last_light_target:
                self._last_light_target = target
                asyncio.create_task(self._send_command({"c": "l", "v": target}))

        return result

    async def brake(self) -> dict:
        """Active brake — immediately stop motor."""
        self._last_light_target = "off"
        return await self._send_command({"c": "b"})

    async def stop(self) -> dict:
        """Stop motor (coast to zero)."""
        return await self.set_motor(0)

    # ========== Servo Control ==========

    async def set_servo(self, angle: int) -> dict:
        """Set servo angle (0-180, 90=center)."""
        angle = max(0, min(180, int(angle)))
        return await self._send_command({"c": "s", "v": angle})

    async def centre(self) -> dict:
        """Center steering."""
        return await self.set_servo(90)

    # ========== Gear Control ==========

    async def set_gear(self, on: bool) -> dict:
        """Set gear on/off."""
        return await self._send_command({"c": "g", "v": "on" if on else "off"})

    async def toggle_gear(self) -> dict:
        """Toggle gear."""
        return await self._send_command({"c": "g", "v": "toggle"})

    # ========== Lights Control ==========

    async def set_lights(self, status: str) -> dict:
        """Set lights: front/back/both/off."""
        return await self._send_command({"c": "l", "v": status})

    async def lights_front(self) -> dict:
        return await self.set_lights("front")

    async def lights_back(self) -> dict:
        return await self.set_lights("back")

    async def lights_both(self) -> dict:
        return await self.set_lights("both")

    async def lights_off(self) -> dict:
        return await self.set_lights("off")

    # ========== Display ==========

    async def send_text(self, text: str, icon: str = None) -> dict:
        """Display text on OLED."""
        cmd = {"c": "t", "v": text}
        if icon:
            cmd["i"] = icon
        return await self._send_command(cmd)

    # ========== Status & Sensors ==========

    async def status(self) -> dict:
        """Get current status (motor, servo, gear, lights)."""
        result = await self._send_command({"c": "st"})
        if result.get("ok"):
            st = result.get("st", {})
            return {
                "success": True,
                "motor_speed": st.get("m", 0),
                "servo_angle": st.get("s", 90),
                "gear_on": bool(st.get("g", 0)),
                "lights": st.get("l", "off"),
            }
        return {"success": False, "error": result.get("e", "unknown")}

    async def get_sensors(self) -> dict:
        """One-shot read of all sensors."""
        result = await self._send_command({"c": "sns"})
        if result.get("ok"):
            return result.get("sns", {})
        return {"error": result.get("e", "unknown")}

    async def get_accelerometer(self) -> dict:
        """Get accelerometer data (from sensor push or one-shot)."""
        sensors = await self.get_sensors()
        accel = sensors.get("accel")
        if accel:
            return {
                "success": True,
                "tilt": {"pitch": accel["p"], "roll": accel["r"]},
                "orientation": accel["o"],
            }
        return {"success": False, "message": "Accelerometer not available"}

    async def get_tof(self) -> dict:
        """Get ToF distance data."""
        sensors = await self.get_sensors()
        tof = sensors.get("tof")
        if tof:
            result = {
                "success": True,
                "left_distance_cm": tof.get("l"),
                "right_distance_cm": tof.get("r"),
            }
            if "a" in tof:
                result["angle_degrees"] = tof["a"]
            return result
        return {"success": False, "message": "ToF not available"}

    async def get_ultrasonic(self) -> dict:
        """Get ultrasonic rear distance."""
        sensors = await self.get_sensors()
        ultra = sensors.get("ultra")
        if ultra:
            return {
                "success": True,
                "distance_cm": ultra.get("d"),
                "in_range": ultra.get("ir", False),
            }
        return {"success": False, "message": "Ultrasonic not available"}

    async def get_imu(self) -> dict:
        """Get full IMU data (accel + gyro + tilt) for crash detection."""
        result = await self._send_command({"c": "imu"})
        if result.get("ok") and result.get("imu"):
            return result["imu"]
        return {"error": result.get("e", "unavailable")}

    async def get_proximity_guard(self) -> dict:
        """Get proximity guard status."""
        return await self._send_command({"c": "pg"})

    # ========== Sensor Subscription ==========

    async def subscribe_sensors(self, interval_ms: int = 100,
                                callback: Callable[[dict], None] = None) -> dict:
        """
        Subscribe to sensor push from Pico.
        
        Args:
            interval_ms: Push interval (minimum 50ms)
            callback: Function called with each sensor dict
        """
        self._sensor_callback = callback
        return await self._send_command({"c": "sub", "ms": interval_ms})

    async def unsubscribe_sensors(self) -> dict:
        """Stop sensor push."""
        self._sensor_callback = None
        return await self._send_command({"c": "unsub"})


# ═══════════════════════════════════════════════════════════════════
# SYNC WRAPPER — for existing threaded/blocking code
# ═══════════════════════════════════════════════════════════════════

class PicarWsClientSync:
    """
    Synchronous wrapper around PicarWsClient.
    
    Runs the async event loop in a background thread.
    Provides the same API as PicarClient (REST) for drop-in replacement.
    """

    def __init__(self, ip: str = PICAR_IP, port: int = 5000):
        self._async_client = PicarWsClient(ip, port)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self.auto_lights = True
        self._latest_sensors: Optional[dict] = None
        self._sensor_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._async_client.connected

    @property
    def last_rtt_ms(self) -> float:
        return self._async_client.last_rtt_ms

    def connect(self, timeout: float = 5.0) -> bool:
        """Connect (starts background event loop thread)."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # Wait for connection
        future = asyncio.run_coroutine_threadsafe(
            self._async_client.connect(timeout), self._loop
        )
        return future.result(timeout=timeout + 1)

    def disconnect(self):
        """Disconnect and stop background loop."""
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._async_client.disconnect(), self._loop
            )
            try:
                future.result(timeout=3)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _run_loop(self):
        """Background thread running the async event loop."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout=5.0):
        """Run async coroutine from sync context."""
        if not self._loop or not self._loop.is_running():
            return {"ok": 0, "e": "not connected"}
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except Exception as e:
            return {"ok": 0, "e": str(e)}

    # ========== Motor ==========
    def set_motor(self, speed: int) -> dict:
        self._async_client.auto_lights = self.auto_lights
        return self._call(self._async_client.set_motor(speed))

    def brake(self) -> dict:
        return self._call(self._async_client.brake())

    def stop(self) -> dict:
        return self._call(self._async_client.stop())

    # ========== Servo ==========
    def set_servo(self, angle: int) -> dict:
        return self._call(self._async_client.set_servo(angle))

    def centre(self) -> dict:
        return self._call(self._async_client.centre())

    # ========== Gear ==========
    def set_gear(self, on: bool) -> dict:
        return self._call(self._async_client.set_gear(on))

    def toggle_gear(self) -> dict:
        return self._call(self._async_client.toggle_gear())

    # ========== Lights ==========
    def set_lights(self, status: str) -> dict:
        return self._call(self._async_client.set_lights(status))

    def lights_front(self) -> dict:
        return self._call(self._async_client.lights_front())

    def lights_back(self) -> dict:
        return self._call(self._async_client.lights_back())

    def lights_both(self) -> dict:
        return self._call(self._async_client.lights_both())

    def lights_off(self) -> dict:
        return self._call(self._async_client.lights_off())

    # ========== Display ==========
    def send_text(self, text: str, icon: str = None) -> dict:
        return self._call(self._async_client.send_text(text, icon))

    def clear_display(self) -> dict:
        return self._call(self._async_client.send_text(""))

    # ========== Status & Sensors ==========
    def status(self) -> dict:
        return self._call(self._async_client.status())

    def get_sensors(self) -> dict:
        return self._call(self._async_client.get_sensors())

    def get_accelerometer(self) -> dict:
        return self._call(self._async_client.get_accelerometer())

    def get_imu(self) -> dict:
        """Get full IMU data for crash/stall detection."""
        return self._call(self._async_client.get_imu())

    def get_tof(self) -> dict:
        return self._call(self._async_client.get_tof())

    def get_ultrasonic(self) -> dict:
        return self._call(self._async_client.get_ultrasonic())

    def get_all_sensors(self) -> dict:
        """Get all sensors (compatible with PicarClient API)."""
        sensors = {}
        try:
            sensors['accelerometer'] = self.get_accelerometer()
        except Exception as e:
            sensors['accelerometer'] = {'available': False, 'error': str(e)}
        try:
            sensors['tof'] = self.get_tof()
        except Exception as e:
            sensors['tof'] = {'available': False, 'error': str(e)}
        try:
            sensors['ultrasonic'] = self.get_ultrasonic()
        except Exception as e:
            sensors['ultrasonic'] = {'available': False, 'error': str(e)}
        return sensors

    # ========== Sensor Subscription ==========
    def subscribe_sensors(self, interval_ms: int = 100,
                          callback: Callable[[dict], None] = None) -> dict:
        """Subscribe to sensor push. Callback runs in background thread."""
        def _cb(data):
            with self._sensor_lock:
                self._latest_sensors = data
            if callback:
                callback(data)

        return self._call(
            self._async_client.subscribe_sensors(interval_ms, _cb)
        )

    def unsubscribe_sensors(self) -> dict:
        return self._call(self._async_client.unsubscribe_sensors())

    @property
    def latest_sensors(self) -> Optional[dict]:
        """Get most recent sensor push data (non-blocking)."""
        with self._sensor_lock:
            return self._latest_sensors


# ═══════════════════════════════════════════════════════════════════
# TERMINAL KEYBOARD CONTROL
# ═══════════════════════════════════════════════════════════════════

def main():
    """Terminal keyboard control using WebSocket client."""
    import argparse
    parser = argparse.ArgumentParser(description="Picar WebSocket remote control")
    parser.add_argument("--ip", type=str, default=PICAR_IP,
                        help=f"Pico IP address (default: {PICAR_IP})")
    parser.add_argument("--port", type=int, default=5000,
                        help="Pico port (default: 5000)")
    parser.add_argument("--speed", type=int, default=75,
                        help="Forward/reverse motor speed (0-100, default 75)")
    parser.add_argument("--left-angle", type=int, default=45,
                        help="Servo angle for left steering (default 45)")
    parser.add_argument("--right-angle", type=int, default=135,
                        help="Servo angle for right steering (default 135)")
    args = parser.parse_args()

    state = {
        'speed': max(0, min(100, args.speed)),
        'left_angle': max(0, min(180, args.left_angle)),
        'right_angle': max(0, min(180, args.right_angle)),
    }

    client = PicarWsClientSync(args.ip, args.port)

    print(f"Connecting to Picar via WebSocket at ws://{args.ip}:{args.port}/ws ...")
    if not client.connect():
        print("Connection failed, will retry in background...")
        # Wait a bit for reconnect
        time.sleep(3)
        if not client.connected:
            print("✗ Could not connect. Is the Pico running main_ws.py?")
            return

    try:
        s = client.status()
        if s.get('success'):
            gear_str = "LOW" if s.get('gear_on') else "OFF"
            print(f"✓ Connected. Motor: {s['motor_speed']}, "
                  f"Servo: {s['servo_angle']}°, Gear: {gear_str}")
        else:
            print(f"✓ Connected (status: {s})")
    except Exception as e:
        print(f"⚠️  Connected but status error: {e}")

    def adjust_speed(delta):
        state['speed'] = max(0, min(100, state['speed'] + delta))
        return {"message": f"Speed: ±{state['speed']}"}

    def adjust_steering(delta):
        state['left_angle'] = max(0, min(90, state['left_angle'] - delta))
        state['right_angle'] = min(180, max(90, state['right_angle'] + delta))
        return {"message": f"Steering: L={state['left_angle']}° R={state['right_angle']}°"}

    def toggle_auto_lights():
        client.auto_lights = not client.auto_lights
        return {"message": f"Auto lights: {'ON' if client.auto_lights else 'OFF'}"}

    commands = {
        "w": ("Forward",           lambda: client.set_motor(state['speed'])),
        "s": ("Reverse",           lambda: client.set_motor(-state['speed'])),
        "a": ("Left",              lambda: client.set_servo(state['left_angle'])),
        "d": ("Right",             lambda: client.set_servo(state['right_angle'])),
        "c": ("Centre servo",      lambda: client.centre()),
        " ": ("Stop",              lambda: client.stop()),
        "x": ("Brake",             lambda: client.brake()),
        "+": ("Speed +5",          lambda: adjust_speed(5)),
        "=": ("Speed +5",          lambda: adjust_speed(5)),
        "-": ("Speed -5",          lambda: adjust_speed(-5)),
        "]": ("Steering +5°",      lambda: adjust_steering(5)),
        "[": ("Steering -5°",      lambda: adjust_steering(-5)),
        "f": ("Lights front",      lambda: client.lights_front()),
        "b": ("Lights back",       lambda: client.lights_back()),
        "l": ("Lights both",       lambda: client.lights_both()),
        "o": ("Lights off",        lambda: client.lights_off()),
        "t": ("Toggle auto lights", lambda: toggle_auto_lights()),
        "g": ("Toggle gear",       lambda: client.toggle_gear()),
        "?": ("Status",            lambda: client.status()),
        "1": ("Accelerometer",     lambda: client.get_accelerometer()),
        "2": ("ToF sensors",       lambda: client.get_tof()),
        "3": ("Ultrasonic",        lambda: client.get_ultrasonic()),
        "4": ("All sensors",       lambda: client.get_all_sensors()),
        "q": ("Quit",              None),
    }

    print("\n" + "=" * 70)
    print("PICAR WEBSOCKET REMOTE CONTROL")
    print("=" * 70)
    print(f"\n  Protocol: WebSocket (persistent connection)")
    print(f"  Server:   ws://{args.ip}:{args.port}/ws")
    print(f"\nMovement: W/S=Fwd/Rev  A/D=Left/Right  C=Centre  SPACE=Stop  X=Brake")
    print(f"Tuning:   +/-=Speed({state['speed']})  [/]=Steering")
    print(f"Lights:   F=Front  B=Back  L=Both  O=Off  T=AutoToggle")
    print(f"Gear:     G=Toggle")
    print(f"Sensors:  1=Accel  2=ToF  3=Ultra  4=All  ?=Status")
    print(f"Exit:     Q=Quit")
    print("=" * 70)
    print("\nReady for commands...\n")

    import tty, termios

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            key = sys.stdin.read(1).lower()
            if key not in commands:
                continue

            label, action = commands[key]
            if action is None:
                client.stop()
                print("\r\n✓ Stopped. Goodbye.\n")
                break

            try:
                result = action()
                # Format output
                if key == "4":
                    print("\r\n" + "=" * 50, end="\r\n")
                    for k, v in result.items():
                        print(f"  {k}: {v}", end="\r\n")
                    print("=" * 50, end="\r\n")
                elif isinstance(result, dict):
                    msg = result.get('message', result.get('e', str(result)))
                    rtt = f" [{client.last_rtt_ms:.0f}ms]" if client.last_rtt_ms else ""
                    print(f"\r{label}: {msg}{rtt}" + " " * 20)
                else:
                    print(f"\r{label}: {result}" + " " * 20)

            except Exception as e:
                print(f"\r✗ Error: {e}")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        client.disconnect()


if __name__ == "__main__":
    main()
