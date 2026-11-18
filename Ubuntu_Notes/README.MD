
#  Bruck setup: STM32MP157-DK1 camera and Braid in WSL2

This guide runs strand-cam on an STM32MP157-DK1 and braid-run on Ubuntu 22.04.5 x86_64 under WSL2. It uses one USB camera, registered as cam0, at 640×400 MJPEG with 30 FPS requested.

The one-camera test verified capture, registration, camera data delivery, FakeSync, and a live preview. It did not validate sustained 30 FPS, hardware timestamps, extrinsic calibration, multi-camera 3D tracking, or the Mac/Khepos connection.

## 1. Identify your terminals and source branch

- WSL is your Ubuntu build terminal. In the tested setup, it shows usl@Vengeance or bash-5.1$.
- MP157 is your SSH session on the board. In the tested setup, it shows root@stm32mp1.
- Keep the USB camera plugged into the MP157.
- The tested board address was 10.4.8.64. If it changes, replace it in the SSH and SCP commands below.
- Use the WSL checkout at ~/Bruck on branch stm32mp157-strand-cam. Commit 3d62aca5 contains the ARM fixes. It was committed locally and was not pushed. A fresh clone of eva alone will not include those fixes.

WSL — verify the checkout and connect to the board:

cd "$HOME/Bruck"
git status --short --branch
git log -1 --oneline
ssh root@10.4.8.64

MP157 — verify the architecture, loader, and camera:

uname -m
ls -l /lib/ld-linux-armhf.so.3
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext

Expect armv7l, the ARM hard-float loader, and MJPG at 640x400 and 30.000 fps. The tested camera also advertised 1280×800 at 30 FPS, but it overloaded the board with a live preview. /dev/video0 was the capture node. If the USB device enumeration changes, verify the node again.

## 2. Install the matching ST SDK in WSL

Skip this section if the SDK is already installed under ~/stm32mp1-sdk/4.0.1 and the compiler check below works.

1. In the Windows browser, open the STM32MP1 Developer Package page (https://www.st.com/en/embedded-software/stm32mp1dev.html).
2. Under MP1-DEV-x86, choose Select version → 4.0.0. ST calls the ecosystem release 4.0.0; the installer inside is Yocto Kirkstone 4.0.1.
3. Save SDK-x86_64-stm32mp1-openstlinux-5.15-yocto-kirkstone-mp1-v22.06.15.tar.xz in WSL's ~/Downloads. The download may have an en. prefix.

WSL — install without sudo:

(
  cd "$HOME/Downloads" || exit
  release=openstlinux-5.15-yocto-kirkstone-mp1-v22.06.15
  archive="SDK-x86_64-stm32mp1-$release.tar.xz"
  [ -f "$archive" ] || archive="en.$archive"

  xz -t "$archive" &&
  tar -xf "$archive" &&
  env -u LD_LIBRARY_PATH bash \
    "stm32mp1-$release/sdk/st-image-weston-openstlinux-weston-stm32mp1-x86_64-toolchain-4.0.1-$release.sh" \
    -d "$HOME/stm32mp1-sdk/4.0.1"
)

WSL — verify the compiler:

(
  unset LD_LIBRARY_PATH
  source "$HOME/stm32mp1-sdk/4.0.1/environment-setup-cortexa7t2hf-neon-vfpv4-ostl-linux-gnueabi" &&
  $CC -dumpmachine &&
  $CC -print-sysroot
)

Expect arm-ostl-linux-gnueabi and the SDK's Cortex-A7 sysroot. If the installer previously printed post-relocate-setup.sh: Failed to source despite reporting success, the setup was incomplete. In the tested installation, ROS Humble had set LD_LIBRARY_PATH, and the CMake and Meson setup hooks had to be completed in a clean environment.

## 3. Prepare the Rust and frontend toolchain

Use a WSL terminal with Rustup, Cargo, Clang/libclang, and pkg-config available. The tested machine had libclang 14.

WSL — install the Rust targets and Trunk:

rustup target add armv7-unknown-linux-gnueabihf wasm32-unknown-unknown &&
cargo install trunk --version '~0.21.0' --locked &&
trunk --version

The tested Trunk version was 0.21.14.

WSL — fetch dependencies and cache Trunk's frontend tools:

cd "$HOME/Bruck" &&
cargo fetch --locked &&
cd strand-cam/yew_frontend &&
env -u LD_LIBRARY_PATH -u NO_COLOR trunk build --release --dist dist

Run this in a WSL terminal with internet access. The nested frontend build runs Cargo offline, so the crate cache matters. Trunk separately downloads tools such as wasm-bindgen and wasm-opt.

## 4. Build both executables

Skip a build if you already have the corresponding executable from the intended source revision.

WSL — cross-build strand-cam for the MP157:

(
  cd "$HOME/Bruck" || exit
  unset LD_LIBRARY_PATH NO_COLOR

  source "$HOME/stm32mp1-sdk/4.0.1/environment-setup-cortexa7t2hf-neon-vfpv4-ostl-linux-gnueabi" || exit

  export CC_armv7_unknown_linux_gnueabihf="$CC"
  export CXX_armv7_unknown_linux_gnueabihf="$CXX"
  export AR_armv7_unknown_linux_gnueabihf="$AR"
  export CARGO_TARGET_ARMV7_UNKNOWN_LINUX_GNUEABIHF_LINKER="${CC%% *}"
  export CARGO_TARGET_ARMV7_UNKNOWN_LINUX_GNUEABIHF_RUSTFLAGS="-C link-arg=--sysroot=$SDKTARGETSYSROOT -C link-arg=-mfloat-abi=hard"
  export BINDGEN_EXTRA_CLANG_ARGS_armv7_unknown_linux_gnueabihf="--sysroot=$SDKTARGETSYSROOT"
  export PKG_CONFIG_SYSROOT_DIR_armv7_unknown_linux_gnueabihf="$SDKTARGETSYSROOT"
  export PKG_CONFIG_PATH_armv7_unknown_linux_gnueabihf="${PKG_CONFIG_PATH:-}"
  export PKG_CONFIG_LIBDIR_armv7_unknown_linux_gnueabihf="${PKG_CONFIG_LIBDIR:-$SDKTARGETSYSROOT/usr/lib/pkgconfig:$SDKTARGETSYSROOT/usr/share/pkgconfig}"

  unset CC CXX AR CFLAGS CXXFLAGS CPPFLAGS LDFLAGS
  unset PKG_CONFIG_SYSROOT_DIR PKG_CONFIG_PATH PKG_CONFIG_LIBDIR

  cargo build --locked --release --target armv7-unknown-linux-gnueabihf \
    -p strand-cam --bin strand-cam &&
  file target/armv7-unknown-linux-gnueabihf/release/strand-cam
)

Expect a 32-bit ARM ELF executable with interpreter /lib/ld-linux-armhf.so.3. The build uses the ST compiler and sysroot for native ARM dependencies and bundles the web UI.

WSL — build braid-run for native Ubuntu:

cd "$HOME/Bruck" &&
env -u LD_LIBRARY_PATH -u NO_COLOR \
  cargo build --locked --release --target x86_64-unknown-linux-gnu \
  -p braid-run --bin braid-run

## 5. Deploy and verify strand-cam

WSL — copy the binary for a first deployment:

ssh root@10.4.8.64 'test ! -e /home/root/strand-cam && test ! -L /home/root/strand-cam' &&
scp -O "$HOME/Bruck/target/armv7-unknown-linux-gnueabihf/release/strand-cam" \
  root@10.4.8.64:/home/root/strand-cam

The guard prevents overwriting an existing deployment. If /home/root/strand-cam is already the build you want, skip the copy. For a new build, stop the old process and copy the new binary under a different filename so you can test it while keeping the previous version.

MP157 — verify the deployed binary:

/home/root/strand-cam --help
/home/root/strand-cam --camera-backend webcam --list-cameras

The tested USB camera appeared twice with the same name. The entry with serial 0 corresponded to the working numeric selector 0.

Optional MP157 test — capture before involving Braid:

DISABLE_VERSION_CHECK=1 /home/root/strand-cam \
  --no-browser --camera-backend webcam --camera-name 0 \
  --capture-width 640 --capture-height 400 --capture-fps 30 \
  --http-server-addr 127.0.0.1:3440

Look for acquired first frame: 640x400, then press Ctrl+C to release the camera.

## 6. Start Braid with a one-camera config

The checked-in config/pucks.toml expects two cameras and enables DeviceTimestamp. For this smoke test, create a separate temporary config with cam0, FakeSync, and fixed ports. Fixed ports let the tunnels and relay stay in place across a Braid restart.

WSL terminal 1 — create the test config and start Braid:

cd "$HOME/Bruck"
test_config=$(mktemp /tmp/bruck-cam0.XXXXXX.toml)

cat > "$test_config" <<'TOML'
[mainbrain]
http_api_server_addr = "0.0.0.0:8396"
model_server_addr = "127.0.0.1:8397"
lowlatency_camdata_udp_port = 47912
output_base_dirname = "~/BRAID-DATA"

[trigger]
trigger_type = "FakeSync"
framerate = 30.0

[[cameras]]
name = "cam0"
start_backend = "remote"
http_server_addr = "127.0.0.1:36055"
TOML

DISABLE_VERSION_CHECK=1 \
  ./target/x86_64-unknown-linux-gnu/release/braid-run "$test_config"

Leave Braid running. Find the Not starting remote camera "cam0" line and keep its full --braid-url handy. It contains the access token. Port 8396 is the Braid web UI and camera API. Port 8397 serves tracking results.

## 7. Start the SSH tunnels and UDP relay

The tested router put the board and PC on different subnets. The board could not reach the Windows port forward, even though the forward worked from Windows itself. WSL could reach the MP157 over SSH, so the working setup used the following three paths. It did not depend on the Windows port forward.

WSL terminal 2 — open both HTTP tunnels:

ssh -NT -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:18396:127.0.0.1:8396 \
  -L 127.0.0.1:36055:127.0.0.1:36055 \
  root@10.4.8.64

Leave this terminal open. An idle prompt after authentication is expected. The reverse tunnel lets the board reach Braid. The local forward lets Braid reach the camera's HTTP service once strand-cam starts. If an earlier tunnel still owns one of these ports, stop that tunnel before starting this combined one.

MP157 — test the path to Braid:

curl --noproxy '*' -sS --max-time 8 -o /dev/null \
  -w 'Braid HTTP status: %{http_code}\n' http://127.0.0.1:18396/

Expect 401 because this check sends no token. 000 means no HTTP response arrived.

WSL terminal 3 — start the UDP relay:

ssh -T root@10.4.8.64 python3 -u - <<'MP157_RELAY' | python3 -u -c '
import socket
import sys

stream = sys.stdin.buffer
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
    while True:
        header = stream.read(2)
        if len(header) != 2:
            break
        size = int.from_bytes(header, "big")
        packet = stream.read(size)
        if len(packet) != size:
            break
        udp.sendto(packet, ("127.0.0.1", 47912))
'
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
    udp.bind(("127.0.0.1", 47912))
    print("MP157 UDP relay ready on 127.0.0.1:47912", file=sys.stderr, flush=True)
    while True:
        packet, _ = udp.recvfrom(65535)
        sys.stdout.buffer.write(len(packet).to_bytes(2, "big") + packet)
        sys.stdout.buffer.flush()
MP157_RELAY

Leave this terminal open after it prints MP157 UDP relay ready. Both WSL and the MP157 need Python 3 for this relay. The HTTP tunnels carry TCP; the relay carries the separate camera datagrams.

## 8. Start cam0 and check the live feed

MP157 — read the Braid URL:

read -r braid_url

Press Enter after the command. When it waits for input, paste only the full URL from the current Braid startup log, including ?token=.... Keep port 8396 as printed and press Enter. Keep the token out of shared logs.

MP157 — start the camera after the prompt returns:

DISABLE_VERSION_CHECK=1 /home/root/strand-cam \
  --no-browser \
  --camera-backend webcam \
  --camera-name 0 \
  --camera-id cam0 \
  --braid-url "http://127.0.0.1:18396${braid_url#*:8396}" \
  --capture-width 640 \
  --capture-height 400 \
  --capture-fps 30

The command replaces the URL's host and port with the board's end of the SSH tunnel while keeping the token.

### Verify the setup

1. MP157 logs: Look for acquired first frame: 640x400, Registered camera with Braid, and Sending detected coordinates via UDP to: 127.0.0.1:47912.
2. WSL Braid logs: Look for cam0 being assigned a camera number and All expected cameras synchronized. Check for a successful run without Error sending frame offset.
3. Windows browser: Open Braid's tokenized URL on http://127.0.0.1:8396/. Confirm cam0 appears and the image updates. The direct camera view is at http://127.0.0.1:36055/. Keep only one live preview open when comparing performance.

WSL — optional UDP verification:

sudo timeout 10 tcpdump -ni lo -c 5 'udp dst port 47912'

Packets reaching that port verify relay delivery. Braid's camera and synchronization logs confirm the application processed the stream.

## 9. Troubleshooting and test limits

| Symptom | What to check |
|---|---|
| SDK complains about LD_LIBRARY_PATH | In WSL, unset it before sourcing the SDK. ROS Humble set it on the tested machine. |
| ARM compilation fails in CUDA or UFMF | Verify the checkout contains the ARM fixes in 3d62aca5. Disabling strand-cam default features alone did not remove CUDA. |
| Trunk reports a missing tool or crate | Cache the locked dependencies and build the frontend in an internet-connected WSL terminal. Unset NO_COLOR if Trunk rejects its value. |
| MP157 curl returns 000 | Verify Braid is running and the reverse SSH tunnel is open. Test 127.0.0.1:18396 from the board. |
| Braid logs “no access token” | That is expected for the tokenless curl test. For camera startup, use the full tokenized URL from the current Braid run. |
| Camera registers but no data arrives | Check that the UDP relay is still running and that both ends use port 47912. |
| Braid reports Connection refused sending a frame offset | Confirm the return tunnel is open and the camera is using port 36055. In the original test, opening the tunnel after registration required resending the offset Braid had calculated for that session. Do not reuse an old offset for a new session. |
| Preview lags or the frame queue fills | Use 640×400 MJPEG and one live preview. At 1280×800 the board saturated and dropped frames. At 640×400, closing the preview gave the board about 36% idle CPU in follow-up samples. Braid's embedded preview targets 10 FPS, and the camera page's FPS number is a host-timestamp estimate. |
| Brightness changes sharply as the scene changes | Manual exposure at 9.1 ms gave steadier brightness in the tested lighting. It was a live camera setting, so check it again after restarting or changing lighting. |
| ffmpeg is missing | Capture and live preview still worked. Recording was not validated. |

## 10. Shut down and plan the next test

1. MP157: Press Ctrl+C in the terminal running strand-cam.
2. WSL: Stop Braid, the UDP relay, and the SSH tunnels with Ctrl+C in their respective terminals.
3. For the next run, start Braid → HTTP tunnels → UDP relay → camera. Use the current Braid URL and token. A clean startup with the fixed ports is the recommended repeatability test.
4. Keep the next tests separate. Test sustained FPS and preview latency for performance, the results-server connection for Mac/Khepos integration, and hardware timestamps and calibrated multi-camera capture for 3D tracking.