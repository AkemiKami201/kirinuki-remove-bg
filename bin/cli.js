#!/usr/bin/env node
/*
 * kirinuki launcher.
 *
 * Subcommands:
 *   kirinuki web            Start the web server in the foreground (Ctrl+C to stop)
 *   kirinuki start          Start the server in the background
 *   kirinuki stop           Stop the background server
 *   kirinuki init           Set up the environment and download the default model
 *   kirinuki desktop        Open as a desktop app (Electron)
 *   kirinuki models ls               List models and which are downloaded
 *   kirinuki models pull --model X   Download a model
 *   kirinuki models rm   --model X   Delete a downloaded model
 *   kirinuki update         Update to the latest published version
 *   kirinuki help           Show this help
 *
 * Needs Python 3.11+ already installed (Node cannot install Python for you).
 */
"use strict";

const { spawnSync, spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const net = require("net");

const APP_DIR = path.join(__dirname, "..");
const HOME = process.env.RBL_HOME || path.join(os.homedir(), ".kirinuki");
const VENV_DIR = path.join(HOME, "venv");
const IS_WIN = process.platform === "win32";
const VENV_PY = IS_WIN ? path.join(VENV_DIR, "Scripts", "python.exe") : path.join(VENV_DIR, "bin", "python");
const PID_FILE = path.join(HOME, "server.pid");
const LOG_FILE = path.join(HOME, "server.log");
const PORT = process.env.PORT || "7860";
const HOST = process.env.HOST || "127.0.0.1";
const URL = `http://${HOST}:${PORT}`;
const APP_NAME = "Kirinuki";   // matches the UI and electron/main.js

function log(m) { process.stdout.write(">> " + m + "\n"); }
function err(m) { process.stderr.write(m + "\n"); }
function run(cmd, args, opts) {
  const needsShell = IS_WIN && /\.(cmd|bat)$/i.test(cmd);
  return spawnSync(cmd, args, Object.assign({ stdio: "inherit", shell: needsShell }, opts || {}));
}

let _npmCli;
function npmCli() {
  if (_npmCli !== undefined) return _npmCli;
  const dir = path.dirname(process.execPath);
  const candidates = IS_WIN
    ? [path.join(dir, "node_modules", "npm", "bin", "npm-cli.js")]
    : [
        path.join(dir, "..", "lib", "node_modules", "npm", "bin", "npm-cli.js"),
        path.join(dir, "..", "share", "npm", "bin", "npm-cli.js"),
      ];
  _npmCli = candidates.find((p) => fs.existsSync(p)) || null;
  return _npmCli;
}

function npm(args, opts) {
  const cli = npmCli();
  if (cli) return spawnSync(process.execPath, [cli, ...args], Object.assign({ stdio: "inherit" }, opts || {}));
  return run(IS_WIN ? "npm.cmd" : "npm", args, opts);
}

function findPython() {
  const CHECK = "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)";
  const candidates = IS_WIN
    ? [["py", ["-3"]], ["python", []], ["python3", []]]
    : [["python3", []], ["python", []]];
  for (const [cmd, pre] of candidates) {
    const r = spawnSync(cmd, [...pre, "-c", CHECK]);
    if (r.status === 0) return { cmd, pre };
  }
  return null;
}
function venvHealthy() {
  return fs.existsSync(VENV_PY) && spawnSync(VENV_PY, ["-c", "import sys"]).status === 0;
}
function depsInstalled() {
  return spawnSync(VENV_PY, ["-c", "import fastapi, uvicorn, rembg, PIL, multipart, onnxruntime, psutil"]).status === 0;
}
function ensureSetup() {
  const py = findPython();
  if (!py) {
    err("\nkirinuki needs Python 3.11 or newer.\n" +
        "Install it from https://www.python.org/downloads/ (or `brew install python`) and try again.\n" +
        (IS_WIN ? "On Windows, tick \"Add python.exe to PATH\" in the installer.\n" : ""));
    process.exit(1);
  }
  fs.mkdirSync(HOME, { recursive: true });
  if (!venvHealthy()) {
    log("Creating Python environment (first run)...");
    if (run(py.cmd, [...py.pre, "-m", "venv", VENV_DIR]).status !== 0) { err("Failed to create the virtualenv."); process.exit(1); }
  }
  if (!depsInstalled()) {
    log("Installing dependencies (first run can take 2-5 min)...");
    run(VENV_PY, ["-m", "pip", "install", "--upgrade", "pip"]);
    if (run(VENV_PY, ["-m", "pip", "install", "-r", path.join(APP_DIR, "requirements.txt")]).status !== 0) {
      err("Failed to install dependencies."); process.exit(1);
    }
  }
}
function serverEnv() {
  return Object.assign({}, process.env, { HOST, PORT });
}
function openBrowser(url) {
  const cmd = IS_WIN ? "cmd" : process.platform === "darwin" ? "open" : "xdg-open";
  const args = IS_WIN ? ["/c", "start", "", url] : [url];
  try { spawn(cmd, args, { stdio: "ignore", detached: true }).unref(); } catch (e) { /* ignore */ }
}
function isUp() {
  return new Promise((res) => {
    const req = http.get(URL + "/health", (r) => { r.resume(); res(r.statusCode === 200); });
    req.on("error", () => res(false));
    req.setTimeout(1500, () => { req.destroy(); res(false); });
  });
}
async function waitUp(timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < (timeoutMs || 120000)) { if (await isUp()) return true; await new Promise(r => setTimeout(r, 800)); }
  return false;
}
function portInUse() {
  return new Promise((res) => {
    const s = net.connect({ host: HOST, port: Number(PORT) }, () => { s.destroy(); res(true); });
    s.on("error", () => res(false));
    s.setTimeout(1000, () => { s.destroy(); res(false); });
  });
}

// --- auto-update ---------------------------------------------------------
function currentVersion() { try { return require(path.join(APP_DIR, "package.json")).version; } catch { return null; } }
function semverGt(a, b) {
  const pa = String(a).split(".").map(Number), pb = String(b).split(".").map(Number);
  for (let i = 0; i < 3; i++) { if ((pa[i] || 0) > (pb[i] || 0)) return true; if ((pa[i] || 0) < (pb[i] || 0)) return false; }
  return false;
}
// On launch, say when a newer version exists. Only installs it if the user
// opted in with RBL_AUTO_UPDATE: installing software unasked is not ours to
// decide, and many machines forbid it outright. Note that an install applies
// from the NEXT launch — this process already has the old code loaded.
function autoUpdateIfNewer() {
  if (process.env.RBL_NO_UPDATE) return;
  const cur = currentVersion(); if (!cur) return;
  let latest = null;
  try {
    const r = npm(["view", "kirinuki", "version"], { stdio: "pipe", encoding: "utf8", timeout: 7000 });
    if (r.status === 0) latest = (r.stdout || "").trim();
  } catch { return; }
  if (!latest || !semverGt(latest, cur)) return;
  if (!process.env.RBL_AUTO_UPDATE) {
    log(`A newer version is available (v${cur} -> v${latest}). Run \`kirinuki update\` to install it.`);
    return;
  }
  log(`Updating ${cur} -> ${latest} (applies on the next launch)...`);
  npm(["install", "-g", "kirinuki@latest"]);
}
function readPid() { try { return parseInt(fs.readFileSync(PID_FILE, "utf8").trim(), 10) || 0; } catch { return 0; } }
function pidAlive(pid) { try { process.kill(pid, 0); return true; } catch { return false; } }

// ---- commands ----
async function cmdWeb() {
  if (await isUp()) { log(`Already running — open ${URL}`); openBrowser(URL); return; }
  if (await portInUse()) { err(`Port ${PORT} is already in use by another app. Open ${URL} if that is this app, or start on another port, e.g.  PORT=8000 kirinuki web`); process.exit(1); }
  ensureSetup();
  autoUpdateIfNewer();
  log(`Starting server on ${URL}`);
  log("(Ctrl+C to stop)");
  setTimeout(() => openBrowser(URL), 2500);
  const child = spawn(VENV_PY, [path.join(APP_DIR, "server.py")], { stdio: "inherit", env: serverEnv() });
  try { fs.writeFileSync(PID_FILE, String(child.pid)); } catch {}
  child.on("exit", (code) => { try { fs.unlinkSync(PID_FILE); } catch {} process.exit(code || 0); });
  process.on("SIGINT", () => child.kill("SIGINT"));
  process.on("SIGTERM", () => child.kill("SIGTERM"));
}
async function cmdStart() {
  if (await isUp()) { log(`Already running — open ${URL}`); openBrowser(URL); return; }
  const pid = readPid();
  if (pid && pidAlive(pid)) { log(`Already running (pid ${pid}) on ${URL}`); return; }
  if (await portInUse()) { err(`Port ${PORT} is in use by another app. Use a different PORT, e.g.  PORT=8000 kirinuki start`); process.exit(1); }
  ensureSetup();
  log("Starting server in the background...");
  const out = fs.openSync(LOG_FILE, "a");
  const child = spawn(VENV_PY, [path.join(APP_DIR, "server.py")], { stdio: ["ignore", out, out], env: serverEnv(), detached: true });
  fs.writeFileSync(PID_FILE, String(child.pid));
  child.unref();
  const ok = await waitUp(120000);
  if (ok) { log(`Running on ${URL} (pid ${child.pid})`); log(`Logs: ${LOG_FILE} — stop with: kirinuki stop`); openBrowser(URL); }
  else { log(`Started (pid ${child.pid}); still warming up. Logs: ${LOG_FILE}`); }
  process.exit(0);
}
function cmdStop() {
  const pid = readPid();
  if (!pid || !pidAlive(pid)) { log("No background server is running."); try { fs.unlinkSync(PID_FILE); } catch {} return; }
  try {
    // Windows has no SIGTERM, and the Python process can hold several GB, so
    // end the whole tree rather than leaving it orphaned.
    if (IS_WIN) spawnSync("taskkill", ["/pid", String(pid), "/T", "/F"], { stdio: "ignore" });
    else process.kill(pid, "SIGTERM");
    log(`Stopped server (pid ${pid}).`);
  } catch (e) { err("Could not stop: " + e.message); }
  try { fs.unlinkSync(PID_FILE); } catch {}
}
function cmdInit() {
  ensureSetup();
  log("Downloading the default model...");
  run(VENV_PY, [path.join(APP_DIR, "server.py"), "models", "pull"]);
  log("Ready. Start it with: kirinuki start   (or kirinuki web)");
}
function cmdModels(rest) {
  ensureSetup();
  const r = run(VENV_PY, [path.join(APP_DIR, "server.py"), "models", ...rest]);
  process.exit(r.status || 0);
}
function startMenuDir() {
  return path.join(process.env.APPDATA || os.homedir(), "Microsoft", "Windows", "Start Menu", "Programs");
}
function desktopInstalled() {
  if (process.platform === "darwin")
    return ["/Applications", path.join(os.homedir(), "Applications")]
      .some((b) => fs.existsSync(path.join(b, APP_NAME + ".app")));
  if (process.platform === "linux")
    return fs.existsSync(path.join(os.homedir(), ".local", "share", "applications", "kirinuki.desktop"));
  if (process.platform === "win32")
    return fs.existsSync(path.join(startMenuDir(), APP_NAME + ".lnk"));
  return false;
}

function npmLatest() {
  try {
    const r = npm(["view", "kirinuki", "version"], { stdio: "pipe", encoding: "utf8", timeout: 8000 });
    return r.status === 0 ? (r.stdout || "").trim() : null;
  } catch { return null; }
}

function cmdUpdate() {
  const before = currentVersion();
  log("Checking npm for updates...");
  const latest = npmLatest();
  if (latest && before && !semverGt(latest, before)) {
    log(`Already on the latest version (v${before}). Nothing to update.`);
    return;
  }
  log(`Updating v${before || "?"} -> v${latest || "latest"} ...`);
  const r = npm(["install", "-g", "kirinuki@latest"]);
  if (r.status !== 0) { err("Update failed. If you run it with npx, just use `npx -y kirinuki@latest`."); return; }
  // Not currentVersion(): require() cached package.json before the update, so
  // it would report the version we just replaced.
  log(`Updated to v${latest || "latest"}. It takes effect on the next launch.`);

  if (desktopInstalled()) {
    log("Refreshing the installed desktop app...");
    const ri = run(IS_WIN ? "kirinuki.cmd" : "kirinuki", ["desktop", "install"]);
    if (ri.status !== 0) log("Could not refresh automatically — run `kirinuki desktop install` to update the app.");
  }
}
function pkgVersion() { try { return require(path.join(APP_DIR, "package.json")).version || "0.0.0"; } catch { return "0.0.0"; } }

function electronExecutable(desktopDir) {
  const pkgDir = path.join(desktopDir, "node_modules", "electron");
  if (!fs.existsSync(pkgDir)) return null;
  try {
    const rel = fs.readFileSync(path.join(pkgDir, "path.txt"), "utf8").trim();
    if (rel) {
      const exe = path.join(pkgDir, "dist", rel);
      if (fs.existsSync(exe)) return exe;
    }
  } catch (e) { }
  const fallback = path.join(pkgDir, "dist", IS_WIN ? "electron.exe" : "electron");
  return fs.existsSync(fallback) ? fallback : null;
}


function fetchElectronBinary(desktopDir) {
  const installer = path.join(desktopDir, "node_modules", "electron", "install.js");
  if (!fs.existsSync(installer)) return false;
  log("Downloading the Electron binary (~100 MB)...");
  const r = run(process.execPath, [installer], { cwd: path.dirname(installer) });
  return r.status === 0;
}

function ensureElectron() {
  const desktopDir = path.join(HOME, "desktop");
  let electronBin = electronExecutable(desktopDir);
  if (electronBin) return { desktopDir, electronBin };

  const pkgDir = path.join(desktopDir, "node_modules", "electron");
  let npmResult = { status: 0 };
  if (!fs.existsSync(pkgDir)) {
    log("Installing the desktop runtime (Electron) the first time...");
    fs.mkdirSync(desktopDir, { recursive: true });
    if (!fs.existsSync(path.join(desktopDir, "package.json"))) {
      fs.writeFileSync(path.join(desktopDir, "package.json"), JSON.stringify({ name: "rbl-desktop", private: true }, null, 2));
    }
    npmResult = npm(["install", "electron@latest"], { cwd: desktopDir });
    electronBin = electronExecutable(desktopDir);
    if (electronBin) return { desktopDir, electronBin };
  }

  // The package is present but its binary is not, so npm has nothing left to
  // do and will keep saying "up to date". Run the package's own downloader.
  if (npmResult.status === 0 && fetchElectronBinary(desktopDir)) {
    electronBin = electronExecutable(desktopDir);
    if (electronBin) return { desktopDir, electronBin };
  }

  err("\nCould not set up Electron in " + desktopDir + ".");
  if (npmResult.error) err("  " + npmResult.error.message);
  err("\nElectron is fetched in two steps: the npm package, then a ~100 MB");
  err("binary. The package is here but the binary is missing, which usually");
  err("means the download was interrupted. npm reports \"up to date\" from");
  err("then on and will not retry it, so remove the package and start again:");
  err("  rmdir /s /q \"" + pkgDir + "\"     (Windows)");
  err("  rm -rf \"" + pkgDir + "\"          (macOS/Linux)");
  err("\nBehind a proxy, set it first:");
  err("  npm config set proxy http://your-proxy:port");
  err("\nThe web interface does not need any of this:");
  err("  kirinuki web\n");
  process.exit(1);
}

function patchPlistName(plist) {
  if (!fs.existsSync(plist)) return;
  spawnSync("plutil", ["-replace", "CFBundleName", "-string", APP_NAME, plist]);
  spawnSync("plutil", ["-replace", "CFBundleDisplayName", "-string", APP_NAME, plist]);
}

function cmdDesktop(rest) {
  const sub = (rest[0] || "").toLowerCase();
  if (sub === "install") return cmdDesktopInstall();
  if (sub === "uninstall") return cmdDesktopUninstall();

  ensureSetup();
  autoUpdateIfNewer();
  const { desktopDir, electronBin } = ensureElectron();
  if (process.platform === "darwin") {
    const appBundle = path.join(desktopDir, "node_modules", "electron", "dist", "Electron.app");
    patchPlistName(path.join(appBundle, "Contents", "Info.plist"));
    try { fs.utimesSync(appBundle, new Date(), new Date()); } catch {}
  }
  log("Opening desktop app...");

  const nodePath = [path.join(desktopDir, "node_modules"), process.env.NODE_PATH]
    .filter(Boolean).join(path.delimiter);
  const childEnv = Object.assign({}, process.env, { RBL_PY: VENV_PY, RBL_APP: APP_DIR, HOST, PORT, NODE_PATH: nodePath });
  delete childEnv.ELECTRON_RUN_AS_NODE;
  delete childEnv.ELECTRON_NO_ATTACH_CONSOLE;
  const child = spawn(electronBin, [path.join(APP_DIR, "electron", "main.js")], {
    stdio: "inherit",
    env: childEnv,
  });
  child.on("exit", (code) => process.exit(code || 0));
}

function appInstallDir() {
  let dir = "/Applications";
  try { fs.accessSync(dir, fs.constants.W_OK); } catch { dir = path.join(os.homedir(), "Applications"); fs.mkdirSync(dir, { recursive: true }); }
  return dir;
}

function cmdDesktopInstall() {
  if (process.platform === "darwin") return installMac();
  if (process.platform === "linux") return installLinux();
  if (process.platform === "win32") return installWindows();
  err("Unsupported platform for install. Use `kirinuki desktop`."); process.exit(1);
}

function installMac() {
  ensureSetup();
  const { desktopDir } = ensureElectron();
  const srcApp = path.join(desktopDir, "node_modules", "electron", "dist", "Electron.app");
  if (!fs.existsSync(srcApp)) { err("Electron runtime not found."); process.exit(1); }

  const destApp = path.join(appInstallDir(), APP_NAME + ".app");
  log(`Building ${path.basename(destApp)} ...`);
  run("rm", ["-rf", destApp]);
  if (run("cp", ["-R", srcApp, destApp]).status !== 0) { err("Copy failed."); process.exit(1); }

  // Inject our app into the bundle (Electron loads Contents/Resources/app).
  const resApp = path.join(destApp, "Contents", "Resources", "app");
  fs.mkdirSync(resApp, { recursive: true });
  run("cp", [path.join(APP_DIR, "electron", "main.js"), path.join(resApp, "main.js")]);
  run("cp", [path.join(APP_DIR, "server.py"), path.join(resApp, "server.py")]);
  run("cp", [path.join(APP_DIR, "requirements.txt"), path.join(resApp, "requirements.txt")]);
  run("cp", ["-R", path.join(APP_DIR, "static"), path.join(resApp, "static")]);
  fs.writeFileSync(path.join(resApp, "package.json"), JSON.stringify(
    { name: "kirinuki", productName: APP_NAME, version: pkgVersion(), main: "main.js" }, null, 2));

  // Icon + name.
  const icns = path.join(APP_DIR, "static", "app-icon.icns");
  if (fs.existsSync(icns)) run("cp", [icns, path.join(destApp, "Contents", "Resources", "electron.icns")]);
  const plist = path.join(destApp, "Contents", "Info.plist");
  patchPlistName(plist);
  spawnSync("plutil", ["-replace", "CFBundleIdentifier", "-string", "app.removebackground.local", plist]);

  try { fs.utimesSync(destApp, new Date(), new Date()); } catch {}
  spawnSync("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister", ["-f", destApp]);

  log(`Installed: ${destApp}`);
  log("Opening it now. You'll also find it in Launchpad / Applications.");
  run("open", [destApp]);
}

function installLinux() {
  ensureSetup();
  ensureElectron();   // so the launcher works when clicked
  const appsDir = path.join(os.homedir(), ".local", "share", "applications");
  fs.mkdirSync(appsDir, { recursive: true });
  const icon = path.join(APP_DIR, "static", "logo.png");
  // A missing icon is written into the .desktop without complaint and shows up
  // as a blank launcher - which is how the logo-dark.png typo went unnoticed.
  if (!fs.existsSync(icon)) log("Icon file missing (" + icon + "); the launcher will have no icon.");
  const exec = `"${process.execPath}" "${path.join(APP_DIR, "bin", "cli.js")}" desktop`;
  const entry = [
    "[Desktop Entry]", "Type=Application", `Name=${APP_NAME}`,
    "Comment=Remove image backgrounds locally", `Exec=${exec}`, `Icon=${icon}`,
    "Terminal=false", "Categories=Graphics;Utility;", "",
  ].join("\n");
  const f = path.join(appsDir, "kirinuki.desktop");
  fs.writeFileSync(f, entry);
  try { fs.chmodSync(f, 0o755); } catch {}
  spawnSync("update-desktop-database", [appsDir]);
  log("Installed launcher: " + f);
  log(`Look for '${APP_NAME}' in your application menu.`);
}

function installWindows() {
  ensureSetup();
  ensureElectron();
  const ico = path.join(APP_DIR, "static", "app-icon.ico");
  const cli = path.join(APP_DIR, "bin", "cli.js");
  const programs = path.join(process.env.APPDATA || os.homedir(), "Microsoft", "Windows", "Start Menu", "Programs");
  try { fs.mkdirSync(programs, { recursive: true }); } catch {}
  const lnk = path.join(programs, APP_NAME + ".lnk");
  if (!fs.existsSync(ico)) log("Icon file missing (" + ico + "); Windows will use a generic one.");
  const esc = (s) => s.replace(/'/g, "''");
  // Stop + try/catch: a COM failure otherwise prints its error and still exits
  // 0, which reported success with no shortcut written.
  const ps = [
    "$ErrorActionPreference = 'Stop';",
    "try {",
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('" + esc(lnk) + "');",
    "$s.TargetPath = '" + esc(process.execPath) + "';",
    "$s.Arguments = '\"" + esc(cli) + "\" desktop';",
    "$s.IconLocation = '" + esc(ico) + "';",
    "$s.WorkingDirectory = '" + esc(APP_DIR) + "';",
    "$s.Save()",
    "} catch { Write-Error $_; exit 1 }",
  ].join(" ");

  // powershell.exe first, then pwsh: a Windows install always has the former,
  // but a stripped-down or PowerShell 7-only box may only have the latter.
  let r = { status: null, error: new Error("powershell not started") };
  for (const shell of ["powershell", "pwsh"]) {
    r = spawnSync(shell, ["-NoProfile", "-Command", ps], { stdio: "inherit" });
    if (!r.error) break;
  }

  // Three different failures, each with its own fix, so say which one it was.
  if (r.error) {
    err("Could not run PowerShell (" + r.error.message + ").");
  } else if (r.status !== 0) {
    err("PowerShell could not create the shortcut (exit " + r.status + ").");
  } else if (!fs.existsSync(lnk)) {
    // Reported success but wrote nothing - worth catching rather than trusting.
    err("PowerShell reported success but " + lnk + " was not created.");
  } else {
    log("Installed Start Menu shortcut: " + lnk);
    return;
  }
  err("You can still run the app with:  kirinuki desktop");
}

function cmdDesktopUninstall() {
  let removed = false;
  // macOS
  for (const base of ["/Applications", path.join(os.homedir(), "Applications")]) {
    const a = path.join(base, APP_NAME + ".app");
    if (fs.existsSync(a)) { run("rm", ["-rf", a]); removed = true; log("Removed " + a); }
  }
  // Linux
  const dl = path.join(os.homedir(), ".local", "share", "applications", "kirinuki.desktop");
  if (fs.existsSync(dl)) { try { fs.unlinkSync(dl); removed = true; log("Removed " + dl); } catch {} }
  // Windows
  const wl = path.join(startMenuDir(), APP_NAME + ".lnk");
  if (fs.existsSync(wl)) { try { fs.unlinkSync(wl); removed = true; log("Removed " + wl); } catch {} }
  if (!removed) log("No installed app/shortcut found.");
}
function cmdHelp() {
  process.stdout.write(`
kirinuki — remove image backgrounds locally

Usage:
  kirinuki web                     Start the web server (foreground, Ctrl+C to stop)
  kirinuki start                   Start the server in the background
  kirinuki stop                    Stop the background server
  kirinuki init                    Set up and download the default model
  kirinuki desktop                 Open as a desktop app (Electron)
  kirinuki desktop install         Install as an app (macOS .app / Linux launcher / Windows shortcut)
  kirinuki desktop uninstall       Remove the installed app/shortcut
  kirinuki models ls               List models and which are downloaded
  kirinuki models pull --model X   Download a model
  kirinuki models rm   --model X   Delete a downloaded model
  kirinuki update                  Update to the latest version
  kirinuki version                 Print the installed version
  kirinuki help                    Show this help

Env: HOST (default 127.0.0.1), PORT (default 7860)
`);
}

function main() {
  const argv = process.argv.slice(2);
  const cmd = (argv[0] || "web").toLowerCase();
  const rest = argv.slice(1);
  switch (cmd) {
    case "web": case "serve": return cmdWeb();
    case "start": case "up": return void cmdStart();
    case "stop": case "down": return cmdStop();
    case "init": case "setup": return cmdInit();
    case "desktop": case "app": return cmdDesktop(rest);
    case "models": case "model": return cmdModels(rest);
    case "update": case "upgrade": return cmdUpdate();
    case "version": case "--version": case "-v": return void process.stdout.write((currentVersion() || "unknown") + "\n");
    case "help": case "-h": case "--help": return cmdHelp();
    default:
      err(`Unknown command: ${cmd}\n`); cmdHelp(); process.exit(1);
  }
}
main();
