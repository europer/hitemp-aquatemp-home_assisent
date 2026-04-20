#!/usr/bin/env python3
import hashlib, logging, time, threading, requests
import paho.mqtt.client as mqtt

HITEMP_BASE_URL = "https://cloud.linked-go.com:449/crmservice/api"
HITEMP_USER     = "myemail@mymail.com"
HITEMP_PASSWORD = "mypassword"
DEVICE_CODE     = "0C8FARC56834"
MQTT_HOST       = "192.168.x.x"
MQTT_PORT       = 1883
MQTT_USER       = "mymqttuser"
MQTT_PASS       = "mymqttpassword"
POLL_INTERVAL   = 60
TEMP_MIN, TEMP_MAX = 38.0, 60.0
HYS_MIN, HYS_MAX   = 1.0, 10.0
DEFAULT_TEMP, DEFAULT_HYS, DEFAULT_MODE = "55", "5", "eco"
MODE_MAP = {"eco": "2", "hybrid": "3", "fast": "4"}
MODE_REVERSE = {v: k for k, v in MODE_MAP.items()}
TOPIC_SET_TEMP="hitemp/set/temperature"; TOPIC_SET_HYS="hitemp/set/hysteresis"; TOPIC_SET_MODE="hitemp/set/mode"
TOPIC_STATE_TEMP="hitemp/state/temperature"; TOPIC_STATE_HYS="hitemp/state/hysteresis"; TOPIC_STATE_MODE="hitemp/state/mode"
TOPIC_STATE_T_BOTTOM="hitemp/state/temp_bottom"; TOPIC_STATE_T_TOP="hitemp/state/temp_top"
TOPIC_STATE_T_COIL="hitemp/state/temp_coil"; TOPIC_STATE_T_AMB="hitemp/state/temp_ambient"
TOPIC_STATE_STATUS="hitemp/state/status"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("/home/clawbot/hitemp_proxy.log"), logging.StreamHandler()])
log = logging.getLogger("hitemp")

_token = None; _token_time = 0; _token_lock = threading.Lock(); TOKEN_TTL = 3300
last_state = {}; mqtt_client = None

def _do_login():
    global _token, _token_time
    pw = hashlib.md5(HITEMP_PASSWORD.encode()).hexdigest()
    try:
        r = requests.post(f"{HITEMP_BASE_URL}/app/user/login",
            json={"userName": HITEMP_USER, "password": pw},
            headers={"Content-Type": "application/json"}, timeout=15)
        res = r.json()
        if res.get("isReusltSuc"):
            _token = res["objectResult"]["x-token"]; _token_time = time.time()
            log.info("HiTemp login OK"); return True
        log.error(f"Login failed: {res.get('error_msg')}"); _token = None; return False
    except Exception as e:
        log.error(f"Login exception: {e}"); _token = None; return False

def get_token():
    global _token, _token_time
    with _token_lock:
        if _token is None or (time.time() - _token_time) > TOKEN_TTL:
            _do_login()
        return _token

def hitemp_read():
    global _token
    tok = get_token()
    if not tok: return None
    try:
        r = requests.post(f"{HITEMP_BASE_URL}/app/device/getDataByCode",
            json={"deviceCode": DEVICE_CODE, "protocalCodes": ["R01","R03","Mode","T01","T02","T03","T04"]},
            headers={"Content-Type": "application/json", "x-token": tok}, timeout=15)
        res = r.json()
        if not res.get("isReusltSuc"):
            log.error(f"Read failed: {res.get('error_msg')}")
            with _token_lock: _token = None
            return None
        return {i["code"]: i["value"] for i in res.get("objectResult", [])}
    except Exception as e:
        log.error(f"Read exception: {e}"); return None

def hitemp_write(params):
    tok = get_token()
    if not tok: return False, "login_failed"
    pl = [{"deviceCode": DEVICE_CODE, "protocolCode": k, "value": str(v)} for k, v in params.items()]
    try:
        r = requests.post(f"{HITEMP_BASE_URL}/app/device/control",
            json={"param": pl}, headers={"Content-Type": "application/json", "x-token": tok}, timeout=15)
        res = r.json()
        if res.get("isReusltSuc"): log.info(f"Write OK: {params}"); return True, "ok"
        log.error(f"Write failed: {res.get('error_msg')}"); return False, res.get("error_msg","unknown")
    except Exception as e:
        log.error(f"Write exception: {e}"); return False, str(e)

def mqtt_publish(topic, value):
    if mqtt_client: mqtt_client.publish(topic, str(value), retain=True)

def publish_state(data):
    global last_state
    mode_str = MODE_REVERSE.get(data.get("Mode",""), DEFAULT_MODE)
    new_state = {
        TOPIC_STATE_TEMP: data.get("R01", DEFAULT_TEMP), TOPIC_STATE_HYS: data.get("R03", DEFAULT_HYS),
        TOPIC_STATE_MODE: mode_str, TOPIC_STATE_T_BOTTOM: data.get("T01",""),
        TOPIC_STATE_T_TOP: data.get("T02",""), TOPIC_STATE_T_COIL: data.get("T03",""),
        TOPIC_STATE_T_AMB: data.get("T04",""),
    }
    for t, v in new_state.items():
        if last_state.get(t) != v: mqtt_publish(t, v); log.info(f"Published {t}: {v}")
    last_state = new_state

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        log.info("MQTT connected")
        for t in [TOPIC_SET_TEMP, TOPIC_SET_HYS, TOPIC_SET_MODE]: client.subscribe(t)
        log.info("Subscribed to command topics")
    else: log.error(f"MQTT connect failed: {reason_code}")

def on_message(client, userdata, msg):
    topic = msg.topic; payload = msg.payload.decode().strip()
    log.info(f"MQTT cmd: {topic} = {payload}"); status = "ok"
    if topic == TOPIC_SET_TEMP:
        try:
            val = float(payload); clamped = max(TEMP_MIN, min(TEMP_MAX, val))
            if clamped != val: status = f"clamped:{clamped}"
            ok, txt = hitemp_write({"R01": clamped})
            if not ok: status = f"error:{txt}"
        except ValueError: status = "error:invalid_value"
    elif topic == TOPIC_SET_HYS:
        try:
            val = float(payload); clamped = max(HYS_MIN, min(HYS_MAX, val))
            if clamped != val: status = f"clamped:{clamped}"
            ok, txt = hitemp_write({"R03": clamped})
            if not ok: status = f"error:{txt}"
        except ValueError: status = "error:invalid_value"
    elif topic == TOPIC_SET_MODE:
        mode = payload.lower()
        if mode not in MODE_MAP: status = f"error:unknown_mode:{mode}"
        else:
            ok, txt = hitemp_write({"Mode": MODE_MAP[mode]})
            if not ok: status = f"error:{txt}"
    mqtt_publish(TOPIC_STATE_STATUS, status)
    time.sleep(2)
    data = hitemp_read()
    if data: publish_state(data)

def poll_loop():
    while True:
        try:
            data = hitemp_read()
            if data: publish_state(data); mqtt_publish(TOPIC_STATE_STATUS, "ok")
            else: mqtt_publish(TOPIC_STATE_STATUS, "error:read_failed")
        except Exception as e: log.error(f"Poll exception: {e}")
        time.sleep(POLL_INTERVAL)

def main():
    global mqtt_client
    log.info("HiTemp proxy starting")
    get_token()
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hitemp_proxy")
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    mqtt_client.on_connect = on_connect; mqtt_client.on_message = on_message
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()
    threading.Thread(target=poll_loop, daemon=True).start()
    log.info("HiTemp proxy running")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down"); mqtt_client.loop_stop()

if __name__ == "__main__":
    main()
