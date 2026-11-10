"""Settings and auto-detection of the installation (HA, Zigbee2MQTT, radio, how to run zigpy)."""
import copy
import json
import os
import shlex
import subprocess
import sys
import time

from . import util
from .ha import HA, Supervisor

ZIGPY_SIDE = os.path.join(util.REPO_ROOT, "zigpy_side")

DEFAULTS = {
    "state_dir": "state",
    "ha": {"url": None, "token_env": "HA_TOKEN", "verify_tls": True},
    "ha_config_dir": None,           # HA config dir as seen by this tool (auto: /homeassistant, /config)
    "ha_config_dir_in_ha": None,     # the same dir as seen by HA's Python (auto: /config in a container)
    "ha_python": None,               # command that runs HA's Python, e.g. "docker exec -i homeassistant python3"
    "ha_container": "homeassistant",
    "zha_database": None,            # default <ha_config_dir>/zigbee.db (ZHA's default database_path)
    "custom_quirks_dir_in_ha": None,  # default <ha config in HA>/custom_zha_quirks when it exists
    "z2m": {
        "addon_slug": None,          # auto: the installed add-on whose slug/name contains zigbee2mqtt
        "data_dir": None,            # auto: add-on data_path, else <ha_config_dir>/zigbee2mqtt
        "stop_cmd": None,            # for non add-on installs, e.g. "docker stop zigbee2mqtt"
        "start_cmd": None,           # e.g. "docker start zigbee2mqtt"
        "unique_id_suffix": "_zigbee2mqtt",
    },
    "radio": {"port": None, "type": None, "baudrate": None, "flow_control": None},
    "port_check": None,              # docker | proc | none (auto: docker when available)
    "expected_devices": None,        # auto: number of devices written to the preseeded DB
    "energy": {"power": None, "utility_meter": None},
    "soft_counters": [],
    "must_exist": [],
    "helper_reload_domains": ["integration", "generic_thermostat", "template", "group", "utility_meter",
                              "statistics", "derivative", "min_max", "threshold", "filter"],
    # Extra config entry domains to disable while the Z2M entities are removed (on top of the
    # core helpers that follow their source entity), e.g. a custom integration that deletes or
    # rewrites itself when its source entity disappears.
    "protect_domains": [],
    # Domains whose stored device id (a "device" option) is moved to the ZHA device through
    # their own options flow. Other device id references are reported as manual follow-ups.
    "relink_options_domains": ["template"],
}
# Z2M serial.adapter -> zigpy radio type (names of zha's RadioType enum)
Z2M_ADAPTER_TO_RADIO = {"zstack": "znp", "ember": "ezsp", "ezsp": "ezsp", "deconz": "deconz", "zigate": "zigate"}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Env:
    def __init__(self, config_path=None):
        path = config_path or os.environ.get("Z2M2ZHA_CONFIG") or os.path.join(util.REPO_ROOT, "config.json")
        user = json.load(open(path)) if os.path.exists(path) else {}
        self.config_path = path if os.path.exists(path) else None
        self.cfg = _merge(DEFAULTS, user)
        sd = self.cfg["state_dir"]
        self.state_dir = sd if os.path.isabs(sd) else os.path.join(util.REPO_ROOT, sd)
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self._cache = {}
        self.sup = Supervisor()

    # ------------------------------------------------------------ files
    def state(self, name):
        return os.path.join(self.state_dir, name)

    def load_state(self, name, default=None):
        p = self.state(name)
        if not os.path.exists(p):
            if default is not None:
                return default
            hint = ""
            if name == "capture.json":
                hint = (f"\nIf the state dir was lost, copy the newest {self.ha_config_dir}/z2m2zha-backups/"
                        f"capture-*.json to {p}")
            raise SystemExit(f"missing {p}: run the step that creates it first{hint}")
        return json.load(open(p))

    def save_state(self, name, obj, mode=0o600):
        util.write_bytes(self.state(name), json.dumps(obj, indent=1), mode)

    @property
    def ha_config_dir(self):
        if "hcd" not in self._cache:
            d = self.cfg["ha_config_dir"]
            if not d:
                d = next((c for c in ("/homeassistant", "/config") if util.exists(f"{c}/.storage")), None)
            if not d:
                raise SystemExit("cannot find the HA config dir: set ha_config_dir in the config")
            self._cache["hcd"] = d.rstrip("/")
        return self._cache["hcd"]

    def storage(self, name):
        """Read a .storage file (read-only, never written by this tool)."""
        return util.read_json(f"{self.ha_config_dir}/.storage/{name}")

    @property
    def staging(self):
        """Folder inside the HA config dir that both this tool and HA's Python can see."""
        d = f"{self.ha_config_dir}/.z2m2zha"
        if not util.exists(d):
            util.fs("mkdir", "-p", d)
            util.fs("chmod", "700", d)
        return d

    def in_ha(self, local_path):
        """Translate a path under the HA config dir to the path HA's Python sees."""
        if not local_path.startswith(self.ha_config_dir + "/"):
            raise ValueError(f"{local_path} is not under {self.ha_config_dir}")
        return self.ha_config_dir_in_ha + local_path[len(self.ha_config_dir):]

    @property
    def ha_config_dir_in_ha(self):
        d = self.cfg["ha_config_dir_in_ha"]
        if d:
            return d.rstrip("/")
        return "/config" if self._ha_python_is_container() else self.ha_config_dir

    @property
    def zha_database(self):
        return self.cfg["zha_database"] or f"{self.ha_config_dir}/zigbee.db"

    @property
    def custom_quirks_in_ha(self):
        q = self.cfg["custom_quirks_dir_in_ha"]
        if q:
            return q
        local = f"{self.ha_config_dir}/custom_zha_quirks"
        return self.in_ha(local) if util.exists(local) else ""

    # ------------------------------------------------------------ docker / HA python
    @property
    def docker(self):
        if "docker" not in self._cache:
            self._cache["docker"] = next((p for p in (["docker"], ["sudo", "-n", "docker"])
                                          if util.ok([*p, "ps", "-q"])), None)
        return self._cache["docker"]

    @property
    def ha_python(self):
        if "py" not in self._cache:
            cmd = self.cfg["ha_python"]
            if cmd:
                py = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
            elif util.ok([sys.executable, "-c", "import zigpy, zha"]):
                py = [sys.executable]
            elif self.docker and util.ok([*self.docker, "exec", self.cfg["ha_container"], "python3", "-c",
                                          "import zigpy, zha"]):
                py = [*self.docker, "exec", "-i", self.cfg["ha_container"], "python3"]
            else:
                raise SystemExit("cannot find a Python with zigpy + zha. Set ha_python in the config, e.g.\n"
                                 '  "ha_python": "docker exec -i homeassistant python3"')
            self._cache["py"] = py
        return self._cache["py"]

    def _ha_python_is_container(self):
        return "exec" in self.ha_python

    def zigpy_script(self, script, *args, input=None, stream=False, timeout=None, check=True):
        """Run zigpy_side/<script> with HA's Python (same zigpy/zha/quirks versions ZHA uses).

        The code is passed with -c, so nothing has to be copied into the container. Streamed
        output is masked so nothing key-like is ever printed.
        """
        code = open(os.path.join(ZIGPY_SIDE, script)).read()
        cmd = [*self.ha_python, "-c", code, *[str(a) for a in args]]
        if not stream:
            r = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
            if check and r.returncode:
                raise RuntimeError(f"{script} failed (exit {r.returncode}):\n{util.mask(r.stdout[-3000:])}"
                                   f"\n{util.mask(r.stderr[-3000:])}")
            return r.stdout
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE if input else subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        if input:
            p.stdin.write(input)
            p.stdin.close()
        out = []
        for line in p.stdout:
            line = util.mask(line)
            out.append(line)
            print("   " + line, end="", flush=True)
        p.wait(timeout)
        if check and p.returncode:
            raise RuntimeError(f"{script} failed (exit {p.returncode})")
        return "".join(out)

    def ha(self):
        return HA(self.cfg)

    # ------------------------------------------------------------ Zigbee2MQTT
    @property
    def z2m(self):
        if "z2m" not in self._cache:
            self._cache["z2m"] = Z2M(self)
        return self._cache["z2m"]

    @property
    def radio(self):
        """Serial settings for zigpy: from the config, else from Zigbee2MQTT's own settings."""
        if "radio" not in self._cache:
            r = dict(self.cfg["radio"])
            s = self.z2m.serial()
            r["port"] = r["port"] or s.get("port")
            if not r["type"] and s.get("adapter"):
                r["type"] = Z2M_ADAPTER_TO_RADIO.get(s["adapter"])
                if r["type"] is None:
                    raise SystemExit(f"Z2M adapter '{s['adapter']}' has no zigpy radio library. "
                                     "ZHA cannot take this network over.")
            r["baudrate"] = r["baudrate"] or s.get("baudrate") or 115200
            if r["flow_control"] is None and s.get("rtscts"):
                r["flow_control"] = "hardware"
            if not r["port"]:
                raise SystemExit("cannot find the coordinator port: set radio.port in the config")
            if r["port"].startswith("tcp://"):  # Z2M network coordinator syntax -> pyserial/zigpy syntax
                r["port"] = "socket://" + r["port"][len("tcp://"):]
            self._cache["radio"] = r
        return self._cache["radio"]

    # ------------------------------------------------------------ serial port holders
    def port_holders(self):
        port = self.radio["port"]
        if "://" in port:  # network coordinator: nothing to check locally
            return []
        tty = os.path.realpath(port)
        mode = self.cfg["port_check"] or ("docker" if self.docker else "proc")
        probe = f"ls -l /proc/[0-9]*/fd 2>/dev/null | grep -q -- '{tty}'"
        if mode == "none":
            return []
        if mode == "docker":
            names = util.run([*self.docker, "ps", "--format", "{{.Names}}"]).split()
            return [c for c in names if util.ok([*self.docker, "exec", c, "sh", "-c", probe])]
        sh = ["sudo", "-n", "sh", "-c", probe] if util.have_sudo() else ["sh", "-c", probe]
        return ["a local process"] if util.ok(sh) else []

    def wait_port_free(self, seconds):
        for _ in range(seconds):
            if not self.port_holders():
                return True
            time.sleep(1)
        return not self.port_holders()


class Z2M:
    """Control and read Zigbee2MQTT: as a Supervisor add-on, or through configured commands."""

    def __init__(self, env):
        self.env = env
        c = env.cfg["z2m"]
        self.stop_cmd, self.start_cmd = c["stop_cmd"], c["start_cmd"]
        self.slug = c["addon_slug"]
        if not self.slug and not self.stop_cmd and env.sup.available:
            hits = [a for a in env.sup.get("/addons")["addons"]
                    if "zigbee2mqtt" in a["slug"].lower() or "zigbee2mqtt" in a["name"].lower().replace(" ", "")]
            hits.sort(key=lambda a: a.get("state") != "started")
            self.slug = hits[0]["slug"] if hits else None

    def _need_control(self):
        if not self.slug and not (self.stop_cmd and self.start_cmd):
            raise SystemExit("cannot control Zigbee2MQTT: no add-on found. Set z2m.stop_cmd and z2m.start_cmd")

    def info(self):
        return self.env.sup.get(f"/addons/{self.slug}/info") if self.slug else {}

    def is_running(self):
        if self.slug:
            return self.info().get("state") == "started"
        return None  # unknown for command-controlled installs

    def stop(self):
        """Stop Z2M and keep it from starting on its own (add-on boot=manual).

        Returns the previous boot mode, so a temporary stop can restore it with start(boot=...).
        """
        self._need_control()
        if self.slug:
            prev = self.info().get("boot") or "auto"
            self.env.sup.post(f"/addons/{self.slug}/options", {"boot": "manual"})
            self.env.sup.post(f"/addons/{self.slug}/stop")
            return prev
        subprocess.run(self.stop_cmd, shell=True, check=True)
        return None

    def start(self, boot="auto"):
        self._need_control()
        if self.slug:
            self.env.sup.post(f"/addons/{self.slug}/options", {"boot": boot or "auto"})
            self.env.sup.post(f"/addons/{self.slug}/start")
        else:
            subprocess.run(self.start_cmd, shell=True, check=True)

    @property
    def data_dir(self):
        d = self.env.cfg["z2m"]["data_dir"]
        if d:
            return d.rstrip("/")
        if self.slug:
            dp = (self.info().get("options") or {}).get("data_path") or "/config/zigbee2mqtt"
            for prefix in ("/config", "/homeassistant", "/share"):
                if dp == prefix or dp.startswith(prefix + "/"):
                    base = self.env.ha_config_dir if prefix != "/share" else "/share"
                    return base + dp[len(prefix):]
        return f"{self.env.ha_config_dir}/zigbee2mqtt"

    def file(self, name):
        return f"{self.data_dir}/{name}"

    def _yaml(self):
        """Parse Z2M's YAML files with HA's Python (PyYAML). Returns {serial, names}."""
        if "_y" not in self.__dict__:
            files = {}
            for fn in util.run(["sudo", "-n", "ls", self.data_dir] if util.have_sudo() else ["ls", self.data_dir]).split():
                if fn.endswith((".yaml", ".yml")):
                    files[fn] = util.read_text(self.file(fn))
            self._y = json.loads(self.env.zigpy_script("z2m_yaml.py", input=json.dumps(files)))
        return self._y

    def serial(self):
        s = dict(self._yaml().get("serial") or {})
        if self.slug:  # add-on options override configuration.yaml
            s.update({k: v for k, v in ((self.info().get("options") or {}).get("serial") or {}).items() if v})
        return s

    def advanced(self):
        """Non-secret radio settings from Z2M's advanced section (transmit_power, channel)."""
        return self._yaml().get("advanced") or {}

    def friendly_names(self):
        """IEEE (0x...) -> friendly name."""
        return self._yaml().get("names") or {}

    def settings(self):
        """Non-secret Z2M settings the features check needs (options, groups, availability, converters)."""
        y = self._yaml()
        try:
            ls = util.run(["sudo", "-n", "ls", self.file("external_converters")] if util.have_sudo()
                          else ["ls", self.file("external_converters")]).split()
        except Exception:  # noqa: BLE001 (no folder)
            ls = []
        return {k: y.get(k) for k in ("device_options", "global_device_options", "groups", "availability",
                                      "external_converters")} | {
            "external_converter_files": [f for f in ls if f.endswith((".js", ".mjs", ".cjs", ".ts"))]}

    def devices(self):
        """Records of Z2M's database.db (newline-delimited JSON, zigbee-herdsman format)."""
        return [json.loads(ln) for ln in util.read_text(self.file("database.db")).splitlines() if ln.strip()]
