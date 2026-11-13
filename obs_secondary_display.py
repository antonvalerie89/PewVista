"""Serve overlay state and manage the OBS browser-source secondary display.

The controller prepares background/overlay frames, exposes them over a local
HTTP server, and configures or launches the bundled OBS runtime when requested.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import mimetypes
import os
import secrets
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
from shutil import which
import subprocess
import sys
import threading
import time
from typing import Callable
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse
import zipfile

from app_paths import get_app_data_path
from licensing import get_watermark_text
from overlay_layout import SECONDARY_LAYOUT_SIZE_BOOST, build_sized_text_fields, encode_png_bytes, fit_overlay_layout, fit_sectioned_overlay_layout, get_bible_locked_font_value, get_corner_detail_size, get_display_key_size, get_effective_stroke_width, normalize_font_value, render_bible_columns_overlay, render_overlay_image, render_sectioned_overlay_image, render_text_fields_overlay, scale_overlay_font_value
from perf_logging import log_error, log_exception, log_timing

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageOps = None

try:
    import obsws_python as obs
except ImportError:
    obs = None


OBS_INSTALLER_NAME = "OBS-Studio-32.1.2-Windows-x64-Installer.exe"
OBS_PORTABLE_ZIP_NAME = "OBS-Studio-32.1.2-Windows-x64.zip"
OBS_INSTALLER_RELATIVE_PATH = Path("Resourses") / "obs_bootstrap" / OBS_INSTALLER_NAME
OBS_PORTABLE_ZIP_RELATIVE_PATH = Path("Resourses") / "obs_bootstrap" / OBS_PORTABLE_ZIP_NAME
OBS_PORTABLE_DIR_RELATIVE_PATH = Path("Resourses") / "obs_portable"
# Setup and the app fetch the official OBS build; keep in step with install_obs_runtime.ps1.
OBS_DOWNLOAD_URL = "https://github.com/obsproject/obs-studio/releases/download/32.1.2/OBS-Studio-32.1.2-Windows-x64.zip"
OBS_DOWNLOAD_SHA256 = "8d97e4563bd8d22d03e63042aa7dccede1d555c9bd35ce8a9e5019b0d0201bf6"
OBS_RUNTIME_FOLDERS = ("bin", "data", "obs-plugins")
VC_REDIST_DOWNLOAD_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
VC_REDIST_REQUIRED_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll")
OBS_SCENE_NAME = "TheLightSecondaryScene"
OBS_BROWSER_SOURCE_NAME = "TheLightSecondaryBrowser"
OBS_SETTINGS_RELATIVE_PATH = Path("obs_secondary_settings.json")
DEFAULT_OBS_SETTINGS = {
    "host": "127.0.0.1",
    "port": 4455,
    "password": "",
    "auto_install": True,
    "auto_launch": True,
    "projector_monitor_index": 0,
    "obs_executable": "",
    "installer_relative_path": str(OBS_INSTALLER_RELATIVE_PATH),
    "portable_zip_relative_path": str(OBS_PORTABLE_ZIP_RELATIVE_PATH),
    "portable_dir_relative_path": str(OBS_PORTABLE_DIR_RELATIVE_PATH),
    "scene_name": OBS_SCENE_NAME,
    "browser_input_name": OBS_BROWSER_SOURCE_NAME,
}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpg", ".mpeg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}
ALLOWED_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
WM_CLOSE = 0x0010
PREVIEW_REFERENCE_WIDTH = 320
PREVIEW_REFERENCE_HEIGHT = 180
SECONDARY_RENDER_SIZE_BOOST = SECONDARY_LAYOUT_SIZE_BOOST
WELCOME_RENDER_SIZE_BOOST = 1.15
DEFAULT_SCROLL_SPEED = 7
MIN_SCROLL_SPEED = 1
MAX_SCROLL_SPEED = 20
SECONDARY_CORNER_FONT_RATIO = 0.4
SECONDARY_MIN_CORNER_FONT_SIZE = 12


def normalize_scroll_speed_value(scroll_speed: object, default: int = DEFAULT_SCROLL_SPEED) -> int:
    """Normalize named or numeric scroll speed input to the supported range.

    What: Maps Slow/Medium/Fast names or parses a number, then clamps it to 1-20.
    Why included: Settings and API callers may represent speed differently or provide
    values outside the renderer's supported range.
    Where used: Controller overlay state and browser-source update methods.
    Returns: Bounded integer speed; unparseable input uses the supplied default.
    """
    if isinstance(scroll_speed, str):
        normalized_text = scroll_speed.strip().lower()
        if normalized_text == "slow":
            return 5
        if normalized_text == "medium":
            return default
        if normalized_text == "fast":
            return 15
    try:
        numeric_value = int(scroll_speed)
    except Exception:
        numeric_value = default
    return max(MIN_SCROLL_SPEED, min(MAX_SCROLL_SPEED, numeric_value))

SECONDARY_DISPLAY_HTML = """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<style>
html, body {
    margin: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: #000;
    color: #fff;
    font-family: Arial, sans-serif;
}
body {
    position: relative;
}
#stage {
    position: absolute;
    inset: 0;
    background: #000;
}
.media-layer {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    background: #000;
    z-index: 0;
    opacity: 0;
}
.media-layer.active {
    opacity: 1;
}
#overlay {
    position: absolute;
    inset: 0;
    z-index: 2;
    pointer-events: none;
    --overlay-shadow-color: rgba(0, 0, 0, 0.55);
    --overlay-stroke-color: #000000;
}
.overlay-image-layer {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: fill;
    z-index: 0;
    opacity: 0;
}
.overlay-image-layer.active {
    opacity: 1;
}
#mainTextWrap {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
}
#mainText {
    width: 100%;
    box-sizing: border-box;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    text-align: center;
    line-height: 1.15;
    text-shadow: 0 2px 10px var(--overlay-shadow-color);
    -webkit-text-stroke: var(--overlay-outline-width, 1.5px) var(--overlay-stroke-color);
    paint-order: stroke fill;
    will-change: transform;
}
#cornerText {
    position: absolute;
    top: 2.5%;
    right: 3%;
    max-width: 32%;
    text-align: right;
    white-space: pre-wrap;
    text-shadow: 0 2px 10px var(--overlay-shadow-color);
    -webkit-text-stroke: var(--overlay-outline-width, 1.5px) var(--overlay-stroke-color);
    paint-order: stroke fill;
}
.hidden {
    display: none;
}
#licenseMark {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 4%;
    z-index: 10;
    text-align: center;
    color: #ff1a1a;
    font: 900 6vh Arial, sans-serif;
    letter-spacing: 0.08em;
    -webkit-text-stroke: 2px #000000;
    paint-order: stroke fill;
    text-shadow: 0 2px 12px rgba(0, 0, 0, 0.85);
    pointer-events: none;
}
.scrolling {
    animation-name: verse-scroll;
    animation-timing-function: linear;
    animation-iteration-count: infinite;
}
@keyframes verse-scroll {
    from { transform: translateY(105%); }
    to { transform: translateY(-105%); }
}
</style>
</head>
<body>
<div id=\"stage\">
    <img id=\"imageA\" class=\"media-layer\" alt=\"\">
    <img id=\"imageB\" class=\"media-layer\" alt=\"\">
    <video id=\"videoA\" class=\"media-layer\" muted playsinline preload=\"auto\" loop></video>
    <video id=\"videoB\" class=\"media-layer\" muted playsinline preload=\"auto\" loop></video>
    <div id=\"overlay\">
        <img id=\"overlayImageA\" class=\"overlay-image-layer hidden active\" alt=\"\">
        <img id=\"overlayImageB\" class=\"overlay-image-layer hidden\" alt=\"\">
        <div id=\"mainTextWrap\"><div id=\"mainText\"></div></div>
        <div id=\"cornerText\"></div>
    </div>
    <div id=\"licenseMark\" class=\"hidden\"></div>
</div>
<script>
const stateUrl = '/state.json';
const videoLayers = [document.getElementById('videoA'), document.getElementById('videoB')];
const imageLayers = [document.getElementById('imageA'), document.getElementById('imageB')];
const overlay = document.getElementById('overlay');
const overlayImageLayers = [document.getElementById('overlayImageA'), document.getElementById('overlayImageB')];
const mainText = document.getElementById('mainText');
const mainTextWrap = document.getElementById('mainTextWrap');
const cornerText = document.getElementById('cornerText');
const licenseMark = document.getElementById('licenseMark');

let currentRevision = -1;
let currentMediaRevision = -1;
let currentOverlayRevision = -1;
let currentBackgroundPath = '';
let activeImageIndex = 0;
let activeVideoIndex = 0;
let activeOverlayImageIndex = 0;
let activeKind = 'none';
let mediaRetryTimer = null;
const hotImageCache = new Map();
const primaryImageLayer = imageLayers[0];
const primaryOverlayImageLayer = overlayImageLayers[0];

const PREVIEW_REFERENCE_WIDTH = 320;
const PREVIEW_REFERENCE_HEIGHT = 180;
const MIN_FITTED_FONT_SIZE = 4;

function getSecondaryScaleFactor() {
    const widthScale = window.innerWidth / Math.max(1, PREVIEW_REFERENCE_WIDTH);
    const heightScale = window.innerHeight / Math.max(1, PREVIEW_REFERENCE_HEIGHT);
    return Math.max(1, Math.min(widthScale, heightScale));
}

function getStartingFontSize(baseSize) {
    return Math.max(MIN_FITTED_FONT_SIZE, Math.round(baseSize));
}

function fitMainText(text, baseSize, scrollEnabled) {
    const availableWidth = Math.max(1, mainTextWrap.clientWidth);
    const availableHeight = Math.max(1, mainTextWrap.clientHeight);
    const minSize = MIN_FITTED_FONT_SIZE;
    let fittedSize = Math.max(minSize, getStartingFontSize(baseSize));
    mainText.textContent = text;
    for (let candidateSize = fittedSize; candidateSize >= minSize; candidateSize -= 1) {
        mainText.style.fontSize = candidateSize + 'px';
        const fitsWidth = mainText.scrollWidth <= (availableWidth + 1);
        const fitsHeight = mainText.scrollHeight <= (availableHeight + 1);
        if (scrollEnabled ? fitsWidth : (fitsWidth && fitsHeight)) {
            return candidateSize;
        }
    }
    return minSize;
}

function updateTextLayoutBounds(fontSize) {
    const strokeWidth = Math.max(1, Math.round(Math.min(window.innerWidth, window.innerHeight) * 0.004));
    const textPadding = strokeWidth + 18;
    mainTextWrap.style.left = textPadding + 'px';
    mainTextWrap.style.top = textPadding + 'px';
    mainTextWrap.style.right = textPadding + 'px';
    mainTextWrap.style.bottom = textPadding + 'px';
    mainText.style.lineHeight = (fontSize + Math.max(2, Math.round(fontSize * 0.28))) + 'px';
}

function mediaKind(path) {
    const clean = String(path || '').split('?')[0].toLowerCase();
    if (!clean) {
        return 'none';
    }
    if ([".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpg", ".mpeg"].some((ext) => clean.endsWith(ext))) {
        return 'video';
    }
    return 'image';
}

function mediaUrl(path) {
    return '/media?path=' + encodeURIComponent(path);
}

function preloadHotBackgrounds(state) {
    const mediaRevision = Number(state.media_revision || state.revision || 0);
    const hotPaths = Array.isArray(state.hot_background_paths) ? state.hot_background_paths : [];
    const targetKeys = new Set();
    hotPaths.forEach((path) => {
        const value = String(path || '');
        if (!value || mediaKind(value) !== 'image') {
            return;
        }
        const key = value + '::' + String(mediaRevision);
        targetKeys.add(key);
        if (hotImageCache.has(key)) {
            return;
        }
        const preload = new Image();
        preload.decoding = 'async';
        preload.src = mediaUrl(value, mediaRevision);
        hotImageCache.set(key, preload);
    });
    Array.from(hotImageCache.keys()).forEach((key) => {
        if (!targetKeys.has(key)) {
            hotImageCache.delete(key);
        }
    });
}

function resetLayer(layer) {
    if (layer.tagName === 'VIDEO') {
        layer.pause();
        layer.removeAttribute('src');
        layer.load();
    } else {
        layer.removeAttribute('src');
    }
    layer.classList.remove('active');
    layer.style.zIndex = '0';
}

function placeLayerAbove(nextLayer, currentLayer) {
    currentLayer.style.zIndex = '0';
    nextLayer.style.zIndex = '1';
}

function clearMedia() {
    imageLayers.forEach(resetLayer);
    videoLayers.forEach(resetLayer);
    activeKind = 'none';
    currentBackgroundPath = '';
    currentMediaRevision = -1;
}

function scheduleMediaRetry() {
    if (mediaRetryTimer) {
        return;
    }
    mediaRetryTimer = window.setTimeout(() => {
        mediaRetryTimer = null;
        currentRevision = -1;
    }, 1500);
}

function clearOverlayImageLayers() {
    overlayImageLayers.forEach((layer) => {
        layer.classList.remove('active');
        layer.removeAttribute('src');
        layer.style.zIndex = '0';
    });
    activeOverlayImageIndex = 0;
}

function showOverlayImage(state) {
    const nextOverlayRevision = Number(state.overlay_revision || state.revision || 0);
    if (currentOverlayRevision === nextOverlayRevision) {
        return;
    }
    let activated = false;
    const activate = () => {
        if (activated) {
            return;
        }
        activated = true;
        primaryOverlayImageLayer.onload = null;
        primaryOverlayImageLayer.onerror = null;
        primaryOverlayImageLayer.classList.add('active');
        currentOverlayRevision = nextOverlayRevision;
    };
    const fail = () => {
        if (activated) {
            return;
        }
        activated = true;
        primaryOverlayImageLayer.onload = null;
        primaryOverlayImageLayer.onerror = null;
        scheduleMediaRetry();
    };
    const nextUrl = '/overlay.png?rev=' + encodeURIComponent(String(nextOverlayRevision));
    const assignNextLayer = () => {
        primaryOverlayImageLayer.onload = () => activate();
        primaryOverlayImageLayer.onerror = () => fail();
        primaryOverlayImageLayer.src = nextUrl;
        if (primaryOverlayImageLayer.complete) {
            if (typeof primaryOverlayImageLayer.decode === 'function') {
                primaryOverlayImageLayer.decode().then(() => activate()).catch(() => activate());
            } else {
                activate();
            }
        }
    };
    const preload = new Image();
    preload.onload = () => assignNextLayer();
    preload.onerror = () => fail();
    preload.src = nextUrl;
}

function applyText(state) {
    const hasRenderOverride = !!(state.overlay_render_text || state.overlay_render_font);
    const bibleColumns = Array.isArray(state.overlay_columns) ? state.overlay_columns.filter((column) => column && Array.isArray(column.verses)) : [];
    const useOverlayImage = !!state.overlay_image_available && !state.overlay_scroll_enabled;
    const overlayFont = state.overlay_render_font || state.overlay_font || {};
    const family = overlayFont.family || 'Arial';
    const baseSize = Math.max(12, Number(overlayFont.size || 24));
    const style = String(overlayFont.style || 'normal').toLowerCase();
    const color = state.overlay_foreground || '#ffffff';
    const shadowColor = state.overlay_stroke_color || '#000000';
    const weight = style.includes('bold') ? '700' : '400';
    const fontStyle = style.includes('italic') ? 'italic' : 'normal';
    const overlayText = state.overlay_render_text || state.overlay_text || '';
    const cornerValue = state.overlay_corner_text || '';
    const cornerBaseSize = Math.max(2, Number(state.overlay_corner_font_size || 24));
    const cornerDetailSize = Math.max(1, Math.min(cornerBaseSize - 1, Number(state.overlay_corner_detail_font_size ?? (cornerBaseSize - 2))));
    const renderCornerText = (value) => {
        cornerText.replaceChildren();
        String(value).split(/\\r?\\n/).forEach((line, index) => {
            const lineElement = document.createElement('span');
            lineElement.textContent = line;
            lineElement.style.display = 'block';
            lineElement.style.fontSize = (index === 0 ? cornerBaseSize : cornerDetailSize) + 'px';
            cornerText.appendChild(lineElement);
        });
    };
    const scrollEnabled = !!state.overlay_scroll_enabled;
    const outlineWidth = Math.max(0, Number(state.overlay_outline_width ?? 1.5));
    mainText.style.setProperty('--overlay-outline-width', outlineWidth + 'px');
    cornerText.style.setProperty('--overlay-outline-width', outlineWidth + 'px');
    mainText.classList.remove('scrolling');
    mainText.style.animationDuration = '';
    mainText.style.animationDelay = '';
    mainText.style.transform = '';
    mainTextWrap.style.alignItems = 'center';
    overlayImageLayers.forEach((layer) => layer.classList.toggle('hidden', !useOverlayImage));
    mainTextWrap.classList.toggle('hidden', useOverlayImage);
    cornerText.classList.toggle('hidden', useOverlayImage);
    if (useOverlayImage) {
        mainText.textContent = '';
        cornerText.textContent = '';
        showOverlayImage(state);
        overlay.classList.toggle('hidden', false);
        return;
    }
    if (bibleColumns.length > 0) {
        const selectedIndex = Math.max(0, Number(state.selected_verse_index || 0));
        mainTextWrap.innerHTML = '';
        mainTextWrap.style.display = 'flex';
        mainTextWrap.style.alignItems = 'stretch';
        mainTextWrap.style.justifyContent = 'center';
        const columns = document.createElement('div');
        columns.style.display = 'flex';
        columns.style.width = '100%';
        columns.style.height = '100%';
        columns.style.gap = '12px';
        columns.style.padding = '16px';
        columns.style.boxSizing = 'border-box';
        bibleColumns.forEach((column, index) => {
            const panel = document.createElement('div');
            panel.style.flex = '1';
            panel.style.display = 'flex';
            panel.style.flexDirection = 'column';
            panel.style.justifyContent = 'flex-start';
            panel.style.alignItems = 'center';
            panel.style.textAlign = 'center';
            panel.style.padding = '6px 8px';
            panel.style.borderLeft = index > 0 ? '1px solid rgba(255,255,255,0.35)' : 'none';
            panel.style.minWidth = '0';
            const title = document.createElement('div');
            title.textContent = column.title || 'Bible';
            title.style.fontFamily = family;
            title.style.fontWeight = '700';
            title.style.fontSize = Math.max(12, Math.round(baseSize * 0.5)) + 'px';
            title.style.color = color;
            title.style.marginBottom = '10px';
            title.style.textShadow = '0 2px 10px ' + shadowColor;
            title.style.webkitTextStroke = outlineWidth + 'px ' + shadowColor;
            title.style.paintOrder = 'stroke fill';
            const verse = document.createElement('div');
            const verseValues = Array.isArray(column.verses) ? column.verses : [];
            const verseText = verseValues[selectedIndex] || verseValues[0] || '—';
            verse.textContent = verseText;
            verse.style.fontFamily = family;
            verse.style.fontWeight = weight;
            verse.style.fontStyle = fontStyle;
            verse.style.fontSize = Math.max(12, Math.round(baseSize * 0.65)) + 'px';
            verse.style.color = color;
            verse.style.whiteSpace = 'pre-wrap';
            verse.style.lineHeight = '1.2';
            verse.style.overflowWrap = 'anywhere';
            verse.style.textShadow = '0 2px 10px ' + shadowColor;
            verse.style.webkitTextStroke = outlineWidth + 'px ' + shadowColor;
            verse.style.paintOrder = 'stroke fill';
            panel.appendChild(title);
            panel.appendChild(verse);
            columns.appendChild(panel);
        });
        mainTextWrap.appendChild(columns);
        cornerText.style.fontFamily = family;
        cornerText.style.fontWeight = weight;
        cornerText.style.fontStyle = fontStyle;
        cornerText.style.color = color;
        renderCornerText(cornerValue);
        overlay.classList.toggle('hidden', !(bibleColumns.length || cornerValue));
        return;
    }
    currentOverlayRevision = -1;
    clearOverlayImageLayers();
    overlay.style.setProperty('--overlay-shadow-color', shadowColor);
    overlay.style.setProperty('--overlay-stroke-color', shadowColor);
    mainText.style.fontFamily = family;
    mainText.style.fontWeight = weight;
    mainText.style.fontStyle = fontStyle;
    mainText.style.color = color;
    mainText.style.width = '100%';
    updateTextLayoutBounds(baseSize);
    mainText.textContent = overlayText;
    const fittedSize = Math.min(baseSize, hasRenderOverride ? baseSize : fitMainText(overlayText, baseSize, scrollEnabled));
    mainText.style.fontSize = fittedSize + 'px';
    updateTextLayoutBounds(fittedSize);
    cornerText.style.fontFamily = family;
    cornerText.style.fontWeight = weight;
    cornerText.style.fontStyle = fontStyle;
    cornerText.style.color = color;
    renderCornerText(cornerValue);
    if (scrollEnabled) {
        const rawSpeed = state.overlay_scroll_speed;
        const speed = Number.isFinite(Number(rawSpeed)) ? Number(rawSpeed) : (String(rawSpeed || 'Medium').toLowerCase() === 'fast' ? 15 : (String(rawSpeed || 'Medium').toLowerCase() === 'slow' ? 5 : 7));
        const normalizedSpeed = Math.max(1, Math.min(20, speed));
        const speedRatio = (normalizedSpeed - 1) / 19;
        const viewportHeight = Math.max(1, mainTextWrap.clientHeight);
        const scrollDistance = Math.max(viewportHeight * 2, mainText.scrollHeight + (viewportHeight * 2));
        const pixelsPerSecond = 28 + (speedRatio * 92);
        const duration = Math.max(12, Math.min(120, scrollDistance / Math.max(1, pixelsPerSecond)));
        const leadIn = Math.min(4, Math.max(0.6, duration * 0.12));
        void mainText.offsetWidth;
        mainText.classList.add('scrolling');
        mainText.style.animationDuration = duration + 's';
        mainText.style.animationDelay = '-' + leadIn + 's';
        mainTextWrap.style.alignItems = 'flex-start';
    }
    overlay.classList.toggle('hidden', !(overlayText || cornerValue));
}

function showImage(path, state) {
    const nextRevision = Number(state.media_revision || state.revision || 0);
    const nextUrl = mediaUrl(path, nextRevision);
    let activated = false;
    const activate = () => {
        if (activated) {
            return;
        }
        activated = true;
        primaryImageLayer.onload = null;
        primaryImageLayer.onerror = null;
        primaryImageLayer.style.objectFit = primaryImageLayer.naturalWidth < primaryImageLayer.naturalHeight ? 'contain' : 'cover';
        primaryImageLayer.classList.add('active');
        imageLayers[1].classList.remove('active');
        imageLayers[1].removeAttribute('src');
        videoLayers.forEach(resetLayer);
        activeImageIndex = 0;
        activeKind = 'image';
        currentBackgroundPath = String(path || '');
        currentMediaRevision = nextRevision;
        applyText(state);
    };
    const fail = () => {
        if (activated) {
            return;
        }
        activated = true;
        primaryImageLayer.onload = null;
        primaryImageLayer.onerror = null;
        clearMedia();
        applyText(state);
        scheduleMediaRetry();
    };
    const assignNextImage = () => {
        primaryImageLayer.onload = () => activate();
        primaryImageLayer.onerror = () => fail();
        primaryImageLayer.src = nextUrl;
        if (primaryImageLayer.complete) {
            if (typeof primaryImageLayer.decode === 'function') {
                primaryImageLayer.decode().then(() => activate()).catch(() => activate());
            } else {
                activate();
            }
        }
    };
    const preload = new Image();
    preload.onload = () => assignNextImage();
    preload.onerror = () => fail();
    preload.src = nextUrl;
}

function showVideo(path, state) {
    const nextIndex = 1 - activeVideoIndex;
    const nextVideo = videoLayers[nextIndex];
    const currentVideo = videoLayers[activeVideoIndex];
    const activate = () => {
        placeLayerAbove(nextVideo, currentVideo);
        nextVideo.classList.add('active');
        window.requestAnimationFrame(() => {
            currentVideo.classList.remove('active');
        });
        imageLayers.forEach(resetLayer);
        activeVideoIndex = nextIndex;
        activeKind = 'video';
        currentBackgroundPath = String(path || '');
        currentMediaRevision = Number(state.media_revision || state.revision || 0);
        nextVideo.play().catch(() => {});
        applyText(state);
    };
    nextVideo.onloadeddata = () => activate();
    nextVideo.onerror = () => {
        clearMedia();
        applyText(state);
        scheduleMediaRetry();
    };
    nextVideo.pause();
    nextVideo.removeAttribute('src');
    nextVideo.load();
    nextVideo.src = mediaUrl(path, state.media_revision || state.revision);
    nextVideo.currentTime = 0;
    nextVideo.load();
    window.setTimeout(() => {
        if (nextVideo.readyState >= 2) {
            activate();
        }
    }, 350);
}

function applyState(state) {
    if (state) {
        const mark = String(state.license_mark || '');
        if (licenseMark.textContent !== mark) {
            licenseMark.textContent = mark;
        }
        licenseMark.classList.toggle('hidden', !mark);
    }
    if (!state || state.revision === currentRevision) {
        return;
    }
    currentRevision = state.revision;
    if (!state.visible || !state.background_path) {
        clearMedia();
        applyText({overlay_text: '', overlay_corner_text: '', overlay_font: state.overlay_font, overlay_foreground: state.overlay_foreground, overlay_scroll_enabled: false});
        return;
    }
    const kind = mediaKind(state.background_path);
    const nextMediaRevision = Number(state.media_revision || state.revision || 0);
    preloadHotBackgrounds(state);
    const mediaChanged = kind !== activeKind || String(state.background_path || '') !== currentBackgroundPath || nextMediaRevision !== currentMediaRevision;
    if (!mediaChanged) {
        applyText(state);
        return;
    }
    if (kind === 'video') {
        showVideo(state.background_path, state);
        return;
    }
    showImage(state.background_path, state);
}

let statePollInFlight = false;

async function pollState() {
    if (statePollInFlight) {
        return;
    }
    statePollInFlight = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 2000);
    try {
        const response = await fetch(stateUrl, { cache: 'no-store', signal: controller.signal });
        if (!response.ok) {
            return;
        }
        const state = await response.json();
        applyState(state);
    } catch (error) {
        console.debug('secondary display poll failed', error);
    } finally {
        window.clearTimeout(timeout);
        statePollInFlight = false;
    }
}

window.setInterval(pollState, 30);
pollState();
</script>
</body>
</html>
"""


class ObsSecondaryDisplayError(RuntimeError):
    """Report that OBS setup, communication, or output cannot be completed.

    What: Provides a dedicated exception type for actionable secondary-display failures.
    Why included: Callers can distinguish OBS compatibility/runtime errors from unrelated
    filesystem or rendering exceptions.
    Where used: ObsSecondaryDisplayController setup and websocket request paths.
    Returns: Exception instances carry the user-facing failure detail.
    """
    pass


def _resolve_font_path(font_family: str, font_style: str) -> Path | None:
    """What: Resolve supported Windows font family/style names to installed font files.

    Why included: Pillow rendering requires a path to match the configured typeface.
    Where used: _get_pil_font_from_value() before loading the output font.
    Returns: Existing font Path, or None when the family/style is unavailable.
    """
    windows_fonts = Path("C:/Windows/Fonts")
    font_map = {
        "segoe ui": {"normal": "segoeui.ttf", "bold": "segoeuib.ttf", "italic": "segoeuii.ttf", "bold italic": "segoeuiz.ttf"},
        "arial": {"normal": "arial.ttf", "bold": "arialbd.ttf", "italic": "ariali.ttf", "bold italic": "arialbi.ttf"},
        "calibri": {"normal": "calibri.ttf", "bold": "calibrib.ttf", "italic": "calibrii.ttf", "bold italic": "calibriz.ttf"},
        "georgia": {"normal": "georgia.ttf", "bold": "georgiab.ttf", "italic": "georgiai.ttf", "bold italic": "georgiaz.ttf"},
        "times new roman": {"normal": "times.ttf", "bold": "timesbd.ttf", "italic": "timesi.ttf", "bold italic": "timesbi.ttf"},
        "verdana": {"normal": "verdana.ttf", "bold": "verdanab.ttf", "italic": "verdanai.ttf", "bold italic": "verdanaz.ttf"},
    }
    family_fonts = font_map.get(str(font_family).casefold())
    if family_fonts is None:
        return None
    file_name = family_fonts.get(str(font_style).casefold(), family_fonts.get("normal"))
    if not file_name:
        return None
    candidate = windows_fonts / file_name
    return candidate if candidate.exists() else None


def _get_pil_font_from_value(font_value):
    """What: Load a Pillow font for a normalized family/size/style tuple.

    Why included: Browser overlay image generation needs consistent measured glyphs.
    Where used: Overlay render-layout construction for OBS state.
    Returns: Pillow font handle, or None when Pillow/input is unavailable.
    """
    if ImageFont is None or not isinstance(font_value, tuple) or len(font_value) < 2:
        return None
    family = str(font_value[0])
    size = abs(int(font_value[1]))
    style = str(font_value[2]) if len(font_value) > 2 else "normal"
    font_path = _resolve_font_path(family, style)
    if font_path is None:
        try:
            return ImageFont.load_default(size)
        except TypeError:
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(str(font_path), size)
    except OSError:
        return ImageFont.load_default()


def _get_overlay_scale(width: int, height: int) -> float:
    """What: Compute a non-shrinking scale relative to the preview reference size.

    Why included: Overlay text should scale up to the projector viewport but not below
    the configured preview font size.
    Where used: _get_scaled_overlay_font_value().
    Returns: Positive floating-point scale factor.
    """
    width_scale = width / max(1, PREVIEW_REFERENCE_WIDTH)
    height_scale = height / max(1, PREVIEW_REFERENCE_HEIGHT)
    return max(1.0, min(width_scale, height_scale))


def _get_scaled_overlay_font_value(font_value, width: int, height: int):
    """What: Scale a valid font tuple for the requested projector dimensions.

    Why included: Browser-source text fitting should start from the actual output size.
    Where used: _build_overlay_render_layout() and OBS controller state generation.
    Returns: Family/negative-pixel-size/style tuple, or original input if malformed.
    """
    if not isinstance(font_value, tuple) or len(font_value) < 2:
        return font_value
    scale_factor = _get_overlay_scale(width, height)
    base_size = abs(int(font_value[1]))
    scaled_size = max(base_size, round(base_size * scale_factor))
    return (str(font_value[0]), -scaled_size, str(font_value[2]) if len(font_value) > 2 else "normal")


def _get_secondary_corner_font_size(font_value, width: int, height: int) -> int:
    """What: Return the selected corner-label size for an output viewport.

    Why included: Key sizing is user-controlled and shared with local previews.
    Where used: _build_overlay_render_layout() for fitted overlay output.
    Returns: Scaled corner font size in pixels.
    """
    return get_display_key_size(width, height)


def get_browser_outline_width(font_size: int) -> int:
    """What: Convert Pillow's outward outline radius to CSS's full stroke thickness.

    Why included: Scrolling browser text must have the same visible outline as static image overlays.
    Where used: OBS state generation for the browser text and corner-label styles.
    Returns: CSS stroke width in output pixels, including zero when outlines are disabled.
    """
    return 2 * get_effective_stroke_width(font_size)


def _build_overlay_render_layout(text: str, font_value, width: int, height: int, scroll_enabled: bool, serialize_overlay_font: Callable[[object], dict | None]) -> tuple[str, dict | None]:
    """What: Scale, fit, and serialize a text overlay for the browser viewport.

    Why included: The HTML source needs text/font values fitted to OBS's actual canvas.
    Where used: Controller output-state construction and layout regression checks.
    Returns: Fitted text and serialized font mapping.
    """
    normalized_font = normalize_font_value(font_value)
    scaled_font = _get_scaled_overlay_font_value(normalized_font, width, height)
    resolved_background = None
    layout = fit_overlay_layout(
        text,
        scaled_font,
        width,
        height,
        scroll_enabled=scroll_enabled,
        base_image=resolved_background,
        size_boost=SECONDARY_RENDER_SIZE_BOOST,
        corner_font_size=_get_secondary_corner_font_size(normalized_font, width, height),
    )
    return layout.text, serialize_overlay_font(layout.font_value)


def compute_obs_overlay_render_layout(
    text: str,
    font_value,
    width: int,
    height: int,
    scroll_enabled: bool,
    serialize_overlay_font: Callable[[object], dict | None],
) -> tuple[str, dict | None]:
    """Expose the controller's overlay fitting calculation as a small public helper.

    What: Delegates text, font, viewport, scroll, and font-serialization inputs to the
    internal layout builder.
    Why included: Tests and callers can verify browser overlay sizing without starting OBS.
    Where used: OBS layout regression tests and external calculation checks.
    Returns: Fitted text and serialized font mapping.
    """
    return _build_overlay_render_layout(text, font_value, width, height, scroll_enabled, serialize_overlay_font)


class SecondaryStateServer:
    """Serve OBS browser-source state, media, and overlay bytes over loopback HTTP.

    What: Owns a threaded localhost server, revisioned state, and cached transparent PNG.
    Why included: OBS browser sources need a local endpoint for changing queue state and
    media without embedding file contents into websocket requests.
    Where used: ObsSecondaryDisplayController for the configured browser source.
    Returns: Instances expose an ephemeral HTTP port plus update/read/shutdown methods.
    """
    def __init__(self) -> None:
        """What: Initialize locked renderer state and start the loopback HTTP server.

        Why included: Each OBS controller needs a private state endpoint on an available port.
        Where used: ObsSecondaryDisplayController construction.
        Returns: None; starts a daemon serving thread and stores the assigned port.
        """
        self._state_lock = threading.Lock()
        self._overlay_image_png = b""
        self.last_state_poll = time.time()
        self._state = {
            "revision": 0,
            "media_revision": 0,
            "overlay_revision": 0,
            "visible": False,
            "background_path": "",
            "overlay_text": "",
            "overlay_corner_text": "",
            "overlay_font": None,
            "overlay_foreground": "#ffffff",
            "overlay_scroll_enabled": False,
            "overlay_scroll_speed": DEFAULT_SCROLL_SPEED,
            "overlay_scroll_origin": None,
            "hot_background_paths": [],
        }
        handler_cls = self._build_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="ObsSecondaryStateServer", daemon=True)
        self._thread.start()

    def state_poll_age(self) -> float:
        """What: Report seconds since the browser renderer last requested state.

        Why included: A healthy output page polls continuously; silence means it is dead.
        Where used: Mirror output watchdog through the controller callback mapping.
        Returns: Non-negative age in seconds.
        """
        return max(0.0, time.time() - float(self.last_state_poll))

    def is_media_allowed(self, media_path: str) -> bool:
        """What: Allow only the current or preloaded background media to be streamed.

        Why included: Without this check the loopback server would hand any readable file
        on the computer to any local program or account that found its port.
        Where used: /media request handling.
        Returns: True for a listed image/video background, otherwise False.
        """
        def normalize(value: object) -> str:
            """What: Normalize a path for case-insensitive comparison on Windows.

            Why included: Requested and allowed media paths may differ only in case or separators.
            Where used: is_media_allowed().
            Returns: Normalized absolute path text, or an empty string for blank values.
            """
            text = str(value or "").strip()
            return os.path.normcase(os.path.abspath(text)) if text else ""

        requested = normalize(media_path)
        if not requested or Path(requested).suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
            return False
        with self._state_lock:
            allowed = {normalize(self._state.get("background_path"))}
            allowed.update(normalize(path) for path in list(self._state.get("hot_background_paths") or []))
        return requested in allowed

    def _normalized_state_signature(self, state: dict, overlay_image_png: bytes) -> tuple[object, ...]:
        """What: Serialize state and PNG bytes while excluding revision counters.

        Why included: Revisions track changes but must not make equal content appear different.
        Where used: update_state() to suppress redundant updates.
        Returns: Deterministic JSON-and-bytes signature tuple.
        """
        normalized_state = dict(state)
        normalized_state.pop("revision", None)
        normalized_state.pop("media_revision", None)
        normalized_state.pop("overlay_revision", None)
        return (json.dumps(normalized_state, sort_keys=True, separators=(",", ":")), bytes(overlay_image_png))

    def _build_handler(self):
        """What: Create the request-handler class bound to this server instance.

        Why included: HTTP handler instances need access to this server's state and image.
        Where used: SecondaryStateServer.__init__() when constructing ThreadingHTTPServer.
        Returns: BaseHTTPRequestHandler subclass for the loopback endpoints.
        """
        outer = self

        class Handler(BaseHTTPRequestHandler):
            """Route browser-source HTTP requests to state, overlay, and media data.

            What: Implements GET endpoints for the renderer HTML, JSON state, PNG overlay,
            and ranged media delivery.
            Why included: OBS browser sources poll state and fetch changed assets locally.
            Where used: Returned by SecondaryStateServer._build_handler().
            Returns: Handler instances service one HTTP request each.
            """
            @staticmethod
            def _is_client_disconnect(error: OSError) -> bool:
                """What: Recognize common client-abort and Windows socket disconnect errors.

                Why included: Browser refreshes can close requests while media is streaming.
                Where used: _send_bytes() and _send_media() error handling.
                Returns: True for expected client disconnects, otherwise False.
                """
                if isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
                    return True
                return getattr(error, "winerror", None) in {10053, 10054}

            def do_GET(self) -> None:
                """What: Dispatch GET paths to HTML, JSON, PNG, or media responses.

                Why included: The OBS browser source retrieves its live state and assets here.
                Where used: BaseHTTPRequestHandler request loop.
                Returns: None; unknown paths receive HTTP 404.
                """
                parsed = urlparse(self.path)
                if parsed.path in {"/", "/secondary_display.html"}:
                    self._send_bytes(SECONDARY_DISPLAY_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if parsed.path == "/state.json":
                    outer.last_state_poll = time.time()
                    self._send_bytes(json.dumps(outer.get_state(), separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")
                    return
                if parsed.path == "/overlay.png":
                    self._send_overlay_png()
                    return
                if parsed.path == "/media":
                    query = parse_qs(parsed.query)
                    media_path = query.get("path", [""])[0]
                    if not outer.is_media_allowed(media_path):
                        self.send_error(HTTPStatus.FORBIDDEN)
                        return
                    self._send_media(Path(media_path))
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def _send_overlay_png(self) -> None:
                """What: Return the current transparent overlay PNG or a 404 when empty.

                Why included: Overlay imagery is fetched separately from small state JSON.
                Where used: do_GET() for /overlay.png.
                Returns: None; writes the HTTP response.
                """
                payload = outer.get_overlay_image_png()
                if not payload:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(payload, "image/png")

            def _send_bytes(self, payload: bytes, content_type: str) -> None:
                """What: Send a no-cache byte response with its content type and length.

                Why included: State and overlays must refresh immediately when revisions change.
                Where used: HTML, state JSON, and overlay PNG endpoints.
                Returns: None; ignores expected client disconnects and re-raises other I/O errors.
                """
                try:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError as error:
                    if not self._is_client_disconnect(error):
                        raise

            def _send_media(self, media_path: Path) -> None:
                """What: Stream an existing media file, honoring browser byte-range requests.

                Why included: OBS video playback may request partial content rather than
                downloading the entire file before playback.
                Where used: do_GET() for /media?path=... requests.
                Returns: None; sends 404 for absent paths and supports 206 responses.
                """
                if not media_path.exists() or not media_path.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_type, _ = mimetypes.guess_type(str(media_path))
                if not content_type:
                    content_type = "application/octet-stream"
                file_size = media_path.stat().st_size
                range_header = self.headers.get("Range")
                start = 0
                end = max(0, file_size - 1)
                status = HTTPStatus.OK
                if range_header and range_header.startswith("bytes="):
                    # OBS/browser video playback requests byte ranges; clamp them to the file bounds.
                    range_value = range_header.split("=", 1)[1]
                    start_text, _, end_text = range_value.partition("-")
                    if start_text:
                        start = max(0, int(start_text))
                    if end_text:
                        end = min(end, int(end_text))
                    status = HTTPStatus.PARTIAL_CONTENT
                chunk_size = max(0, end - start + 1)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(chunk_size))
                    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                    if status == HTTPStatus.PARTIAL_CONTENT:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.end_headers()
                    # Stream fixed-size chunks so a large media asset never has to fit in memory at once.
                    with media_path.open("rb") as media_file:
                        media_file.seek(start)
                        remaining = chunk_size
                        while remaining > 0:
                            data = media_file.read(min(1024 * 1024, remaining))
                            if not data:
                                break
                            self.wfile.write(data)
                            remaining -= len(data)
                except OSError as error:
                    # A browser closing a seek or playback request is normal; other I/O faults remain visible.
                    if not self._is_client_disconnect(error):
                        raise

            def log_message(self, format: str, *args) -> None:
                """What: Suppress BaseHTTPRequestHandler's per-request console logging.

                Why included: Browser polling is frequent and should not flood application output.
                Where used: Automatically called by the HTTP server for each request.
                Returns: None; intentionally emits no log line.
                """
                return

        return Handler

    def get_state(self) -> dict:
        """What: Return a shallow snapshot of current renderer state under the lock.

        Why included: HTTP handlers must not read a dictionary while another thread replaces it.
        Where used: /state.json endpoint handler.
        Returns: State mapping including revision counters, media/overlay settings, and the license mark.
        """
        with self._state_lock:
            state = dict(self._state)
        state["license_mark"] = get_watermark_text()
        return state

    def get_overlay_image_png(self) -> bytes:
        """What: Return a copy of the current overlay PNG bytes under the state lock.

        Why included: Callers need a stable byte snapshot while state updates may replace it.
        Where used: /overlay.png endpoint handler.
        Returns: PNG bytes, empty when no overlay image has been published.
        """
        with self._state_lock:
            return bytes(self._overlay_image_png)

    def update_state(self, next_state: dict, overlay_image_png: bytes | None = None) -> None:
        """What: Publish changed state and image bytes while advancing relevant revisions.

        Why included: The browser polls revision values to fetch media/overlay only when
        their underlying data changed.
        Where used: ObsSecondaryDisplayController.push_state(), set_hot_backgrounds(), close().
        Returns: None; equal state/image pairs are ignored.
        """
        started_at = time.perf_counter()
        with self._state_lock:
            previous_state = dict(self._state)
            previous_overlay_png = bytes(self._overlay_image_png)
            next_overlay_png = bytes(overlay_image_png or b"")
            if self._normalized_state_signature(previous_state, previous_overlay_png) == self._normalized_state_signature(next_state, next_overlay_png):
                return
            revision = int(self._state.get("revision", 0)) + 1
            self._state = dict(next_state)
            self._state["revision"] = revision
            previous_media_revision = int(previous_state.get("media_revision", 0) or 0)
            previous_overlay_revision = int(previous_state.get("overlay_revision", 0) or 0)
            media_changed = str(previous_state.get("background_path") or "") != str(self._state.get("background_path") or "")
            overlay_changed = previous_overlay_png != next_overlay_png
            self._state["media_revision"] = previous_media_revision + 1 if media_changed else previous_media_revision
            self._state["overlay_revision"] = previous_overlay_revision + 1 if overlay_changed else previous_overlay_revision
            self._overlay_image_png = next_overlay_png
        log_timing("obs.state_server.update_state", (time.perf_counter() - started_at) * 1000.0, threshold_ms=15.0, details=f"visible={int(bool(next_state.get('visible')))}")

    def shutdown(self) -> None:
        """What: Stop the HTTP serving loop and close its listening socket.

        Why included: The localhost endpoint must not outlive its OBS controller.
        Where used: ObsSecondaryDisplayController.close().
        Returns: None; performs orderly ThreadingHTTPServer shutdown.
        """
        self._server.shutdown()
        self._server.server_close()


class ObsSecondaryDisplayController:
    """Manage OBS setup, browser-source configuration, and projector state.

    What: Connects to OBS over obs-websocket, serves browser-source state, creates
    scenes/sources, chooses projector monitors, and renders media/overlay state.
    Why included: External output requires OBS lifecycle and browser-source policy
    separate from the Tk preview controller.
    Where used: Instantiated by create_obs_secondary_mirror_window() for mirror.py.
    Returns: Instances expose methods for state updates, visibility, and cleanup.
    """
    def __init__(
        self,
        app_base_path: Path,
        initial_window_mode: str,
        default_background_path: Path,
        get_display_config: Callable[[], dict[str, object]],
        resolve_background_path: Callable[[Path], Path],
        serialize_overlay_font: Callable[[object], dict | None],
        scroll_speed_options: tuple[str, ...],
        get_scroll_timestamp: Callable[[], float],
        build_hot_background_paths: Callable[[list[Path | str | None]], list[str]],
        preload_backgrounds: Callable[[list[Path | str | None]], None],
    ) -> None:
        """What: Store injected app policies and initialize OBS/browser-source state.

        Why included: Paths, monitor geometry, fonts, and hot-background policy belong
        to the application layer and are supplied as callbacks.
        Where used: create_obs_secondary_mirror_window() when mirror output is enabled.
        Returns: None; starts a local SecondaryStateServer and initializes runtime state.
        """
        self.app_base_path = app_base_path
        self.initial_window_mode = str(initial_window_mode)
        self.default_background_path = default_background_path
        self.get_display_config = get_display_config
        self.resolve_background_path = resolve_background_path
        self.serialize_overlay_font = serialize_overlay_font
        self.scroll_speed_options = scroll_speed_options
        self.get_scroll_timestamp = get_scroll_timestamp
        self.build_hot_background_paths = build_hot_background_paths
        self.preload_backgrounds = preload_backgrounds
        self.settings = self._load_settings()
        self.state_server = SecondaryStateServer()
        self.client = None
        self.browser_source_ready = False
        self.browser_source_settings: dict | None = None
        self.projector_opened = False
        self.closed = False
        self.current_background_path = default_background_path
        self.current_window_mode = self.initial_window_mode
        self.current_overlay_text = ""
        self.current_overlay_corner_text = ""
        self.current_overlay_source = ""
        self.current_overlay_sections: dict[str, object] | None = None
        self.current_overlay_columns: list[dict[str, object]] = []
        self.current_selected_verse_index = 0
        self.current_overlay_font = None
        self.current_overlay_render_text = None
        self.current_overlay_render_font = None
        self.current_overlay_foreground = None
        self.current_overlay_scroll_enabled = False
        self.current_overlay_scroll_speed = DEFAULT_SCROLL_SPEED
        self.current_overlay_scroll_origin = None
        self.current_transition_revision = 0
        self.current_hot_background_paths: list[str] = []
        self._last_push_signature: tuple[object, ...] | None = None

    def _normalized_sections_signature(self) -> tuple[tuple[str, str], ...] | None:
        """What: Convert current overlay sections into a stable sorted tuple.

        Why included: Settings signatures need deterministic comparison for mappings.
        Where used: _build_push_signature() to detect section-content changes.
        Returns: Sorted string key/value pairs, or None when sections are absent.
        """
        if not isinstance(self.current_overlay_sections, dict):
            return None
        return tuple(sorted((str(key), str(value)) for key, value in self.current_overlay_sections.items()))

    def _build_push_signature(self) -> tuple[object, ...]:
        """What: Capture all OBS source, media, overlay, and browser settings inputs.

        Why included: Identical output state should not issue repeated websocket updates.
        Where used: push_state() before composing and publishing renderer state.
        Returns: Deterministic tuple for comparison with the last successful push.
        """
        browser_settings = self._build_browser_settings()
        normalized_font = normalize_font_value(self.current_overlay_font)
        normalized_columns = tuple(
            (
                str(column.get("title") or "Bible"),
                str(column.get("theme_header") or ""),
                tuple(str(value or "").strip() for value in list(column.get("verses") or []) if str(value or "").strip()),
            )
            for column in self.current_overlay_columns[:3]
            if isinstance(column, dict)
        )
        return (
            str(self.current_window_mode or ""),
            str(self.current_background_path or ""),
            str(self.current_overlay_text or ""),
            str(self.current_overlay_corner_text or ""),
            tuple(normalized_font) if isinstance(normalized_font, tuple) else None,
            str(self.current_overlay_foreground or ""),
            bool(self.current_overlay_scroll_enabled),
            int(self.current_overlay_scroll_speed or DEFAULT_SCROLL_SPEED),
            float(self.current_overlay_scroll_origin or 0.0),
            str(self.current_overlay_source or ""),
            self._normalized_sections_signature(),
            int(self.current_transition_revision or 0),
            int(self.current_selected_verse_index or 0),
            normalized_columns,
            tuple(self.current_hot_background_paths),
            json.dumps(browser_settings, sort_keys=True, separators=(",", ":")),
        )

    def _load_settings(self) -> dict:
        """What: Merge persisted OBS options over the supported defaults.

        Why included: Host, websocket credentials, auto-start, paths, and source names
        need stable defaults while preserving user configuration.
        Where used: ObsSecondaryDisplayController.__init__().
        Returns: Settings mapping; unreadable or non-object JSON leaves defaults intact.
        """
        settings = dict(DEFAULT_OBS_SETTINGS)
        settings_path = get_app_data_path(self.app_base_path) / OBS_SETTINGS_RELATIVE_PATH.name
        loaded_settings: dict = {}
        if settings_path.exists():
            try:
                parsed = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    loaded_settings = parsed
                    settings.update(loaded_settings)
                    if "auto_install_obs" in loaded_settings:
                        settings["auto_install"] = bool(loaded_settings.get("auto_install_obs"))
                    if "auto_launch_obs" in loaded_settings:
                        settings["auto_launch"] = bool(loaded_settings.get("auto_launch_obs"))
            except (OSError, json.JSONDecodeError):
                pass
        if not str(settings.get("password") or ""):
            # OBS's remote-control port listens on every network interface, so an empty
            # password would let any device on the church network drive the projector.
            generated_password = secrets.token_urlsafe(24)
            try:
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                settings_path.write_text(json.dumps(dict(loaded_settings, password=generated_password), indent=2), encoding="utf-8")
                settings["password"] = generated_password
            except OSError as error:
                log_exception("obs_secondary_display.generate_password", error)
        return settings

    def _scene_name(self) -> str:
        """What: Read the configured scene name or use the application default.

        Why included: Scene lookup/creation must consistently use one name.
        Where used: _ensure_scene(), browser-source creation, and websocket requests.
        Returns: OBS scene name string.
        """
        return str(self.settings.get("scene_name") or OBS_SCENE_NAME)

    def _browser_source_name(self) -> str:
        """What: Read the configured browser-source name or use its default.

        Why included: OBS source lookup and projector controls must share one identifier.
        Where used: Source setup and projector open/close operations.
        Returns: OBS browser input name string.
        """
        return str(self.settings.get("browser_input_name") or OBS_BROWSER_SOURCE_NAME)

    def _find_obs_executable(self) -> Path | None:
        """What: Search configured, portable, PATH, and standard install locations for OBS.

        Why included: Packaged and user-installed OBS runtimes should both be discoverable.
        Where used: _ensure_obs_executable() before install or portable extraction.
        Returns: Existing obs64.exe path, or None when no candidate exists.
        """
        explicit_value = str(self._launch_setting("obs_executable") or "").strip()
        explicit_path = Path(explicit_value) if explicit_value else None
        if explicit_path and explicit_path.exists():
            bundled_root = self.app_base_path / str(self._launch_setting("portable_dir_relative_path") or OBS_PORTABLE_DIR_RELATIVE_PATH)
            bundled_executable = bundled_root / "bin" / "64bit" / "obs64.exe"
            if explicit_path.resolve() == bundled_executable.resolve():
                writable_executable = self._portable_obs_executable_path()
                if writable_executable.exists():
                    return writable_executable
            return explicit_path
        portable_path = self._portable_obs_executable_path()
        if portable_path.exists():
            return portable_path
        for candidate in (which("obs64.exe"), which("obs64")):
            if candidate:
                return Path(candidate)
        common_candidates = [
            Path(os.environ.get("ProgramFiles", "")) / "obs-studio" / "bin" / "64bit" / "obs64.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "obs-studio" / "bin" / "64bit" / "obs64.exe",
            Path(os.environ.get("LocalAppData", "")) / "Programs" / "obs-studio" / "bin" / "64bit" / "obs64.exe",
        ]
        for candidate in common_candidates:
            if candidate.exists():
                return candidate
        return None

    def _portable_obs_executable_path(self) -> Path:
        """What: Construct the expected obs64.exe path inside the configured portable root.

        Why included: Portable bundle extraction and launch use a predictable directory layout.
        Where used: Runtime discovery and portable-bundle validation.
        Returns: Path to bin/64bit/obs64.exe under the portable directory.
        """
        return self._portable_obs_root_path() / "bin" / "64bit" / "obs64.exe"

    def _portable_obs_root_path(self) -> Path:
        """What: Resolve writable portable OBS storage for source or installed runs.

        Why included: Portable OBS writes config/logs beside its runtime; Program Files is read-only.
        Where used: Executable lookup, portable-path checks, and websocket configuration.
        Returns: Shared-data runtime root for installed apps, or the configured source runtime root.
        """
        if self._uses_shared_data():
            # The shared data folder is writable by every Windows account, so programs must
            # never run from it; each account keeps OBS in its own private profile instead.
            local_root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
            return local_root / "PewVista" / get_app_data_path(self.app_base_path).name / "obs_portable"
        return self.app_base_path / str(self.settings.get("portable_dir_relative_path") or OBS_PORTABLE_DIR_RELATIVE_PATH)

    def _uses_shared_data(self) -> bool:
        """What: Report whether this is an installed app using the shared ProgramData folder.

        Why included: Installed apps must distrust anything in the everyone-writable data folder.
        Where used: OBS runtime location and launch-setting checks.
        Returns: True for installed layouts; False for source/legacy runs with local Data.
        """
        data_directory = get_app_data_path(self.app_base_path)
        return data_directory.resolve() != (self.app_base_path / "Data").resolve()

    def _launch_setting(self, key: str):
        """What: Read a setting that decides which program runs, ignoring it when installed.

        Why included: Installed settings live in the shared data folder; a program path or
        arguments stored there would run under whichever account next uses the live screen.
        Where used: OBS executable discovery, bundled-path resolution, and launch arguments.
        Returns: The configured value for source runs, otherwise None.
        """
        return None if self._uses_shared_data() else self.settings.get(key)

    def _prepare_writable_portable_obs(self) -> None:
        """What: Seed or refresh the installed app's writable portable OBS runtime.

        Why included: Launching bundled portable OBS under Program Files causes configuration access errors.
        Where used: _ensure_obs_executable() before runtime discovery.
        Returns: None; preserves shared config and leaves packaged runtime files untouched.
        """
        bundled_root = self.app_base_path / str(self._launch_setting("portable_dir_relative_path") or OBS_PORTABLE_DIR_RELATIVE_PATH)
        writable_root = self._portable_obs_root_path()
        if bundled_root.resolve() == writable_root.resolve():
            return
        bundled_executable = bundled_root / "bin" / "64bit" / "obs64.exe"
        if not bundled_executable.is_file():
            return
        explicit_value = str(self._launch_setting("obs_executable") or "").strip()
        if explicit_value and Path(explicit_value).resolve() not in {
            bundled_executable.resolve(), self._portable_obs_executable_path().resolve(),
        }:
            return
        writable_executable = self._portable_obs_executable_path()
        if writable_executable.is_file():
            source_stat = bundled_executable.stat()
            target_stat = writable_executable.stat()
            if (source_stat.st_size, source_stat.st_mtime_ns) == (target_stat.st_size, target_stat.st_mtime_ns):
                return
        try:
            writable_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(bundled_root, writable_root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("config"))
            source_config = bundled_root / "config"
            target_config = writable_root / "config"
            if source_config.is_dir() and not target_config.exists():
                shutil.copytree(source_config, target_config)
        except OSError as error:
            raise ObsSecondaryDisplayError(
                f"Could not prepare the writable OBS runtime at {writable_root}. Check free disk space in your user profile and close this app's OBS before retrying."
            ) from error

    def _is_portable_obs_path(self, obs_path: Path) -> bool:
        """What: Check whether an OBS executable resides within the bundled portable root.

        Why included: Portable builds need different launch flags and websocket config setup.
        Where used: _configure_portable_obs_websocket() and _obs_launch_command().
        Returns: True when the resolved executable is the root or one of its descendants.
        """
        portable_root = self._portable_obs_root_path().resolve()
        obs_resolved_path = obs_path.resolve()
        return portable_root == obs_resolved_path or portable_root in obs_resolved_path.parents

    def _portable_obs_websocket_config_path(self) -> Path:
        """What: Resolve obs-websocket's config.json inside the portable OBS profile.

        Why included: Bundled portable OBS needs its websocket server enabled on first launch.
        Where used: _configure_portable_obs_websocket().
        Returns: Path to config/obs-studio/plugin_config/obs-websocket/config.json.
        """
        return self._portable_obs_root_path() / "config" / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"

    def _disable_obs_auto_updates(self) -> None:
        """What: Turn off OBS's own update check in the private OBS profile.

        Why included: PewVista ships and updates its own copy of OBS; OBS's updater
        otherwise shows an update window every time PewVista starts the live screen.
        Where used: _configure_portable_obs_websocket() before every OBS launch.
        Returns: None; failures are logged and never block the live screen.
        """
        profile = self._portable_obs_root_path() / "config" / "obs-studio"
        for ini_name in ("global.ini", "user.ini"):
            ini_path = profile / ini_name
            try:
                lines = ini_path.read_text(encoding="utf-8-sig").splitlines() if ini_path.is_file() else []
                lines = [line for line in lines if not line.strip().lower().startswith("enableautoupdates=")]
                if "[General]" in lines:
                    lines.insert(lines.index("[General]") + 1, "EnableAutoUpdates=false")
                else:
                    lines = ["[General]", "EnableAutoUpdates=false"] + ([""] + lines if lines else [])
                ini_path.parent.mkdir(parents=True, exist_ok=True)
                ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            except OSError as error:
                log_exception("obs_secondary_display.disable_auto_updates", error, details=str(ini_path))

    def _configure_portable_obs_websocket(self, obs_path: Path) -> None:
        """What: Enable the portable websocket server and write configured port/auth settings.

        Why included: The controller cannot connect until the browser source runtime exposes
        its local websocket endpoint.
        Where used: _launch_obs_if_needed() before starting bundled OBS.
        Returns: None; leaves non-portable OBS configuration untouched.
        """
        if not self._is_portable_obs_path(obs_path):
            return
        self._disable_obs_auto_updates()
        config_path = self._portable_obs_websocket_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing_config: dict = {}
        if config_path.exists():
            try:
                loaded_config = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded_config, dict):
                    existing_config = loaded_config
            except (OSError, json.JSONDecodeError):
                existing_config = {}
        password = str(self.settings.get("password") or "")
        existing_config.update(
            {
                "alerts_enabled": bool(existing_config.get("alerts_enabled", False)),
                "auth_required": bool(password),
                "first_load": False,
                "server_enabled": True,
                "server_password": password,
                "server_port": int(self.settings.get("port") or 4455),
            }
        )
        config_path.write_text(json.dumps(existing_config, indent=2), encoding="utf-8")

    def _close_stale_private_obs(self, obs_path: Path) -> None:
        """What: Close PewVista's own OBS copies when they run without PewVista's connection.

        Why included: OBS's updater or another PewVista copy can leave an obs_portable
        instance holding port 4455 with a different connection password, which blocks
        a second OBS launch and causes websocket authentication to fail.
        Where used: _launch_obs_if_needed() before starting OBS, and ObsSecondaryDisplayController.close().
        Returns: None; only processes running PewVista's private portable OBS files are closed.
        """
        if sys.platform != "win32" or not self._is_portable_obs_path(obs_path):
            return
        target = str(Path(obs_path).resolve()).replace("'", "''")
        local_root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "PewVista"
        local_prefix = str(local_root.resolve()).replace("'", "''")
        script = (
            "Get-Process obs64 -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Path -and ("
            f"([IO.Path]::GetFullPath($_.Path) -ieq '{target}') -or "
            f"(([IO.Path]::GetFullPath($_.Path)).StartsWith('{local_prefix}', [StringComparison]::OrdinalIgnoreCase) -and ($_.Path -match '\\\\obs_portable\\\\'))"
            ") } | "
            "ForEach-Object { Stop-Process -Id $_.Id -Force; $_.WaitForExit(10000) | Out-Null }"
        )
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as error:
            log_exception("obs_secondary_display.close_stale_obs", error)

    def _obs_launch_command(self, obs_path: Path) -> tuple[list[str], Path]:
        """What: Build OBS launch arguments and its executable working directory.

        Why included: OBS resolves bundled resources relative to bin/64bit, including when
        the portable switch is enabled.
        Where used: _launch_obs_if_needed() before subprocess.Popen().
        Returns: Command argument list and cwd Path.
        """
        command = [str(obs_path), "--disable-shutdown-check", "--minimize-to-tray"]
        launch_arguments = self._launch_setting("launch_arguments")
        if isinstance(launch_arguments, list):
            command.extend(str(argument) for argument in launch_arguments if str(argument).strip() and str(argument) not in command)
        if self._is_portable_obs_path(obs_path):
            command.append("--portable")
            return command, obs_path.parent
        return command, obs_path.parent

    def _is_obs_websocket_available(self) -> bool:
        """What: Probe the configured OBS websocket host/port with a short socket timeout.

        Why included: Avoid launching a second OBS instance when the server is already active.
        Where used: _launch_obs_if_needed() before and during runtime startup.
        Returns: True when a TCP connection succeeds, otherwise False.
        """
        host = str(self.settings.get("host") or "127.0.0.1")
        port = int(self.settings.get("port") or 4455)
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            return False

    def _extract_portable_obs_bundle(self, zip_path: Path | None = None) -> Path:
        """What: Validate and extract the bundled OBS ZIP, then locate obs64.exe.

        Why included: A portable runtime can be installed without invoking the interactive
        OBS installer, and malformed bundles need a clear compatibility error.
        Where used: _ensure_obs_executable() when the bundled archive is present, and
        _provision_obs_runtime() after downloading the official ZIP.
        Returns: Extracted obs64.exe Path; raises ObsSecondaryDisplayError on failure.
        """
        if zip_path is None:
            zip_path = self.app_base_path / str(self.settings.get("portable_zip_relative_path") or OBS_PORTABLE_ZIP_RELATIVE_PATH)
        if not zip_path.exists():
            raise ObsSecondaryDisplayError(
                f"Bundled OBS portable zip was not found at {zip_path}. This system is not compatible with the OBS secondary display until that bundle is present."
            )
        if not zipfile.is_zipfile(zip_path):
            raise ObsSecondaryDisplayError(
                f"Bundled OBS portable zip at {zip_path} is not a valid zip file. Replace it with the official OBS portable zip before using the OBS secondary display on this system."
            )
        target_dir = self._portable_obs_root_path()
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path) as archive:
                for entry in archive.infolist():
                    relative = Path(entry.filename.replace("\\", "/"))
                    if "config" in relative.parts:
                        continue
                    archive.extract(entry, target_dir)
        except (shutil.ReadError, ValueError, OSError) as error:
            raise ObsSecondaryDisplayError(
                "OBS portable bundle could not be extracted. This system is not compatible with the OBS secondary display until the bundled OBS zip is valid."
            ) from error
        portable_path = self._portable_obs_executable_path()
        if portable_path.exists():
            return portable_path
        nested_candidates = list(target_dir.glob("**/obs64.exe"))
        for candidate in nested_candidates:
            if candidate.name.lower() == "obs64.exe" and candidate.parent.name.lower() == "64bit":
                return candidate
        raise ObsSecondaryDisplayError(
            "OBS portable bundle was extracted, but obs64.exe was not found. This system is not compatible with the OBS secondary display until the bundled OBS zip is corrected."
        )

    def _install_obs_from_bundle(self) -> Path:
        """What: Run the bundled Windows OBS installer and locate the installed executable.

        Why included: Systems without an existing or portable OBS runtime can bootstrap
        the required browser-source environment.
        Where used: _ensure_obs_executable() when auto-install is enabled and no ZIP exists.
        Returns: Installed OBS executable Path; raises an actionable runtime error on failure.
        """
        if os.name != "nt":
            raise ObsSecondaryDisplayError("OBS secondary display only supports Windows in this build.")
        installer_path = self.app_base_path / str(self.settings.get("installer_relative_path") or OBS_INSTALLER_RELATIVE_PATH)
        if not installer_path.exists():
            raise ObsSecondaryDisplayError(f"OBS Studio installer was not found at {installer_path}. This system cannot use the OBS secondary display until that installer is bundled.")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        install_command = [
            str(installer_path),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
        ]
        try:
            result = subprocess.run(
                install_command,
                check=False,
                creationflags=creationflags,
                timeout=900,
            )
        except OSError as error:
            if getattr(error, "winerror", None) != 740:
                raise
            powershell_command = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                (
                    "$process = Start-Process -FilePath '{installer}' -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-' "
                    "-Verb RunAs -Wait -PassThru; "
                    "if ($null -eq $process) {{ exit 1223 }}; "
                    "exit $process.ExitCode"
                ).format(installer=str(installer_path).replace("'", "''")),
            ]
            try:
                result = subprocess.run(
                    powershell_command,
                    check=False,
                    creationflags=creationflags,
                    timeout=900,
                )
            except OSError as elevated_error:
                raise ObsSecondaryDisplayError(
                    "OBS Studio installation requires administrator approval and could not be started. This system is not compatible with the OBS secondary display until the bundled OBS installer is approved."
                ) from elevated_error
        if result.returncode not in {0, 3010}:
            if result.returncode == 1223:
                raise ObsSecondaryDisplayError(
                    "OBS Studio installation was cancelled at the Windows elevation prompt. This system is not compatible with the OBS secondary display until OBS is installed."
                )
            raise ObsSecondaryDisplayError("OBS Studio installation failed. This system is not compatible with the OBS secondary display until OBS installs successfully.")
        obs_path = self._find_obs_executable()
        if obs_path is None:
            raise ObsSecondaryDisplayError("OBS Studio installer completed, but OBS was not found afterwards. This system is not compatible with the OBS secondary display yet.")
        return obs_path

    def _find_installed_obs_root(self) -> Path | None:
        """What: Locate an OBS Studio installation whose program files PewVista can reuse.

        Why included: Copying an existing OBS avoids a 185 MB download; its settings are never touched.
        Where used: _provision_obs_runtime().
        Returns: The OBS root folder, or None when no compatible installation exists.
        """
        bases = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""),
                 str(Path(os.environ.get("LocalAppData", "")) / "Programs")]
        for base in bases:
            if not base:
                continue
            root = Path(base) / "obs-studio"
            plugins = root / "obs-plugins" / "64bit"
            if ((root / "bin" / "64bit" / "obs64.exe").is_file() and (plugins / "obs-websocket.dll").is_file()
                    and (plugins / "obs-browser.dll").is_file()):
                return root
        return None

    def _provision_obs_runtime(self) -> Path:
        """What: Create the private OBS copy when Setup could not, by copying or downloading OBS.

        Why included: The installer no longer ships OBS; an offline install must still work later.
        Where used: _ensure_obs_executable() for installed layouts with no OBS copy.
        Returns: The private obs64.exe Path; raises ObsSecondaryDisplayError on failure.
        """
        target = self._portable_obs_root_path()
        installed_root = self._find_installed_obs_root()
        if installed_root is not None:
            try:
                for folder in OBS_RUNTIME_FOLDERS:
                    shutil.copytree(installed_root / folder, target / folder, dirs_exist_ok=True)
            except OSError as error:
                raise ObsSecondaryDisplayError(f"Could not copy OBS Studio from {installed_root}. Check free disk space and try again.") from error
            if self._portable_obs_executable_path().is_file():
                return self._portable_obs_executable_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        download_path = target.parent / "obs-download.zip.part"
        digest = hashlib.sha256()
        try:
            request = urllib.request.Request(OBS_DOWNLOAD_URL, headers={"User-Agent": "PewVista"})
            with urllib.request.urlopen(request, timeout=60) as response, download_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != OBS_DOWNLOAD_SHA256:
                raise ObsSecondaryDisplayError("The downloaded OBS Studio file failed its security check. Try again later.")
            return self._extract_portable_obs_bundle(download_path)
        except (OSError, urllib.error.URLError) as error:
            raise ObsSecondaryDisplayError(
                "OBS Studio (the secondary-screen player) is not on this PC yet and could not be downloaded. Connect to the internet and start the secondary screen again."
            ) from error
        finally:
            try:
                download_path.unlink()
            except OSError:
                pass

    def _ensure_obs_executable(self) -> Path:
        """What: Return an available OBS executable or install/extract one when allowed.

        Why included: All later websocket and browser-source setup depends on a real runtime.
        Where used: _ensure_client() before attempting a websocket connection.
        Returns: OBS executable Path, or raises ObsSecondaryDisplayError if unavailable.
        """
        self._prepare_writable_portable_obs()
        if (self._uses_shared_data() and not self._portable_obs_executable_path().is_file()
                and not str(self._launch_setting("obs_executable") or "").strip()
                and not (self.app_base_path / str(self.settings.get("portable_zip_relative_path") or OBS_PORTABLE_ZIP_RELATIVE_PATH)).is_file()):
            # Without a private copy, a church's own OBS would be found next, and it lacks
            # PewVista's connection settings; create the private copy first.
            return self._provision_obs_runtime()
        obs_path = self._find_obs_executable()
        if obs_path is not None:
            return obs_path
        if not bool(self.settings.get("auto_install", True)):
            raise ObsSecondaryDisplayError("OBS Studio is required for the secondary display and is not installed on this system.")
        portable_zip_path = self.app_base_path / str(self.settings.get("portable_zip_relative_path") or OBS_PORTABLE_ZIP_RELATIVE_PATH)
        if portable_zip_path.exists():
            return self._extract_portable_obs_bundle()
        try:
            return self._install_obs_from_bundle()
        except ObsSecondaryDisplayError:
            portable_path = self._find_obs_executable()
            if portable_path is not None:
                return portable_path
            return self._extract_portable_obs_bundle()

    def _request(self, request_name: str, request_data: dict | None = None) -> dict:
        """What: Send a raw obs-websocket request and convert failed status into an error.

        Why included: Centralizes connection checks and preserves OBS error codes/comments.
        Where used: Scene/source setup, monitor selection, and projector control methods.
        Returns: Response mapping, or an empty mapping for non-dictionary responses.
        """
        if self.client is None:
            raise ObsSecondaryDisplayError("OBS websocket client is not connected.")
        response = self.client.send(request_name, request_data or {}, raw=True)
        if isinstance(response, dict):
            request_status = response.get("requestStatus")
            if isinstance(request_status, dict) and not bool(request_status.get("result", True)):
                error_code = request_status.get("code")
                error_comment = str(request_status.get("comment") or "").strip()
                message = f"OBS request '{request_name}' failed"
                if error_code is not None:
                    message = f"{message} ({error_code})"
                if error_comment:
                    message = f"{message}: {error_comment}"
                raise ObsSecondaryDisplayError(message)
            return response
        return {}

    def _popen_without_app_dll_directory(self, command: list[str], working_directory: Path) -> None:
        """What: Start OBS without PewVista's own DLL folder in its DLL search path.

        Why included: The packaged app sets its _internal folder as the DLL directory, which
        child processes inherit; OBS then loaded PewVista's VCRUNTIME140.dll and, because OBS
        keeps running, locked it so updates could not replace program files.
        Where used: _launch_obs_if_needed().
        Returns: None; restores the app's DLL directory afterwards.
        """
        frozen_dll_directory = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
        kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None
        if kernel32 is not None and frozen_dll_directory:
            kernel32.SetDllDirectoryW(None)
        try:
            subprocess.Popen(command, cwd=str(working_directory), creationflags=0)
        finally:
            if kernel32 is not None and frozen_dll_directory:
                kernel32.SetDllDirectoryW(str(frozen_dll_directory))

    def _is_vc_redist_available(self, obs_path: Path | None = None) -> bool:
        """What: Check whether the 64-bit Visual C++ runtime DLLs required by OBS are available.

        Why included: Portable OBS Studio fails with missing-DLL popups on clean Windows PCs
        that lack MSVCP140.dll and VCRUNTIME140(_1).dll.
        Where used: _ensure_vc_redist_runtime() before launching OBS.
        Returns: True when every required DLL exists in System32 or beside obs64.exe.
        """
        if os.name != "nt":
            return True
        system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"
        obs_bin_dir = obs_path.parent if obs_path is not None else None
        for dll_name in VC_REDIST_REQUIRED_DLLS:
            if (system_root / dll_name).is_file():
                continue
            if obs_bin_dir is not None and (obs_bin_dir / dll_name).is_file():
                continue
            return False
        return True

    def _ensure_vc_redist_runtime(self, obs_path: Path | None = None) -> None:
        """What: Install the Microsoft Visual C++ 2015-2022 Redistributable (x64) when missing.

        Why included: OBS Studio's portable build does not bundle MSVCP140/VCRUNTIME140 DLLs.
        Where used: _launch_obs_if_needed() before starting obs64.exe.
        Returns: None; raises ObsSecondaryDisplayError if missing and installation cannot succeed.
        """
        if self._is_vc_redist_available(obs_path):
            return
        if not bool(self.settings.get("auto_install", True)):
            raise ObsSecondaryDisplayError(
                "Microsoft Visual C++ 2015-2022 Redistributable (x64) is required for OBS Studio (msvcp140.dll / vcruntime140.dll) and is not installed on this PC."
            )
        temp_root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or str(self._portable_obs_root_path().parent))
        temp_root.mkdir(parents=True, exist_ok=True)
        installer_path = temp_root / f"PewVista-vc_redist.x64-{secrets.token_hex(8)}.exe"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            request = urllib.request.Request(VC_REDIST_DOWNLOAD_URL, headers={"User-Agent": "PewVista"})
            with urllib.request.urlopen(request, timeout=60) as response, installer_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            escaped_installer = str(installer_path).replace("'", "''")
            ps_script = (
                f"$sig = Get-AuthenticodeSignature -LiteralPath '{escaped_installer}'; "
                "if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike '*Microsoft Corporation*') { exit 2 }; "
                f"$p = Start-Process -FilePath '{escaped_installer}' -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -Wait -PassThru; "
                "if ($null -eq $p) { exit 1223 }; "
                "exit $p.ExitCode"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                check=False,
                creationflags=creationflags,
                timeout=300,
            )
            if result.returncode == 2:
                raise ObsSecondaryDisplayError(
                    "The downloaded Microsoft Visual C++ Redistributable failed its digital signature check."
                )
            if not self._is_vc_redist_available(obs_path):
                raise ObsSecondaryDisplayError(
                    "Microsoft Visual C++ 2015-2022 Redistributable (x64) is required by OBS Studio (msvcp140.dll, vcruntime140.dll, vcruntime140_1.dll) and could not be installed automatically."
                )
        except (OSError, urllib.error.URLError, subprocess.SubprocessError) as error:
            if not self._is_vc_redist_available(obs_path):
                raise ObsSecondaryDisplayError(
                    "Microsoft Visual C++ 2015-2022 Redistributable (x64) is missing (msvcp140.dll, vcruntime140.dll, vcruntime140_1.dll) and could not be downloaded or installed. Connect to the internet and try again."
                ) from error
        finally:
            try:
                installer_path.unlink()
            except OSError:
                pass

    def _launch_obs_if_needed(self, obs_path: Path) -> None:
        """What: Connect to an existing websocket or launch OBS and wait for readiness.

        Why included: The browser-source workflow requires a responsive OBS websocket,
        but startup can take time after launching either installed or portable OBS.
        Where used: _ensure_client() after locating the executable.
        Returns: None; stores the connected client or raises when the service times out.
        """
        if not bool(self.settings.get("auto_launch", True)):
            return
        self._ensure_vc_redist_runtime(obs_path)
        # Rewrite the bundled OBS remote-control config on every connection so a password
        # added later still reaches OBS the next time it starts.
        self._configure_portable_obs_websocket(obs_path)
        try:
            if self._is_obs_websocket_available():
                self.client = obs.ReqClient(
                    host=str(self.settings.get("host") or "127.0.0.1"),
                    port=int(self.settings.get("port") or 4455),
                    password=str(self.settings.get("password") or ""),
                    timeout=3,
                )
                self._request("GetVersion")
                return
        except Exception:
            # A stale websocket endpoint is not fatal; launch the configured OBS instance below.
            self.client = None
        self._close_stale_private_obs(obs_path)
        launch_command, working_directory = self._obs_launch_command(obs_path)
        self._popen_without_app_dll_directory(launch_command, working_directory)
        deadline = time.time() + 75.0
        last_error = None
        # OBS may need several seconds to load plugins and expose obs-websocket after process launch.
        while time.time() < deadline:
            try:
                if not self._is_obs_websocket_available():
                    time.sleep(1.0)
                    continue
                self.client = obs.ReqClient(
                    host=str(self.settings.get("host") or "127.0.0.1"),
                    port=int(self.settings.get("port") or 4455),
                    password=str(self.settings.get("password") or ""),
                    timeout=3,
                )
                self._request("GetVersion")
                return
            except Exception as error:
                # Keep polling after transient startup failures, but retain the final cause for diagnostics.
                self.client = None
                last_error = error
                time.sleep(1.0)
        raise ObsSecondaryDisplayError("OBS Studio started but obs-websocket did not become available. Check OBS websocket settings, host, port, and password.") from last_error

    def _ensure_client(self) -> None:
        """What: Validate the websocket dependency and lazily connect to OBS.

        Why included: OBS runtime startup should happen only when browser output is needed.
        Where used: _ensure_scene() and other OBS request workflows.
        Returns: None; raises ObsSecondaryDisplayError when dependency/setup fails.
        """
        if obs is None:
            raise ObsSecondaryDisplayError("Python package obsws-python is required for the OBS secondary display backend.")
        if self.client is not None:
            return
        obs_path = self._ensure_obs_executable()
        self._launch_obs_if_needed(obs_path)
        self._ensure_canvas_frame_rate()

    def _ensure_canvas_frame_rate(self) -> None:
        """What: Raise the OBS canvas to 60 FPS when a lower rate is configured.

        Why included: Scrolling text animates in the browser source, but the projector
        can only show frames at the canvas rate; OBS defaults to 30 FPS.
        Where used: _ensure_client() immediately after a websocket connection exists.
        Returns: None; failures are logged and never block output setup.
        """
        try:
            current = self._request("GetVideoSettings")
            numerator = int(current.get("fpsNumerator") or 0)
            denominator = max(1, int(current.get("fpsDenominator") or 1))
            if numerator / denominator >= 59.0:
                return
            self._request("SetVideoSettings", {
                "fpsNumerator": 60,
                "fpsDenominator": 1,
                "baseWidth": int(current.get("baseWidth") or 1920),
                "baseHeight": int(current.get("baseHeight") or 1080),
                "outputWidth": int(current.get("outputWidth") or 1920),
                "outputHeight": int(current.get("outputHeight") or 1080),
            })
            log_error("obs_secondary_display.canvas_fps", f"Raised OBS canvas from {numerator}/{denominator} to 60 FPS for smooth scrolling.")
        except Exception as error:
            log_exception("obs_secondary_display.canvas_fps", error)

    def _ensure_scene(self) -> None:
        """What: Query OBS scenes and create the configured scene when missing.

        Why included: The browser source needs a known scene to attach to.
        Where used: _ensure_browser_source() before creating or updating the input.
        Returns: None; sends OBS websocket requests when required.
        """
        response = self._request("GetSceneList")
        scene_names = {scene.get("sceneName") for scene in response.get("scenes", []) if isinstance(scene, dict)}
        scene_name = self._scene_name()
        if scene_name not in scene_names:
            self._request("CreateScene", {"sceneName": scene_name})

    def _ensure_browser_source(self) -> None:
        """What: Ensure OBS has the configured browser input in the target scene.

        Why included: The local HTML renderer must be registered as a browser source
        before state and overlay updates can be displayed.
        Where used: _ensure_runtime() during first push_state().
        Returns: None; raises a compatibility error when browser-source support is absent.
        """
        if self.browser_source_ready:
            return
        self._ensure_scene()
        kinds_response = self._request("GetInputKindList")
        available_kinds = set(kinds_response.get("inputKinds", []))
        if "browser_source" not in available_kinds:
            raise ObsSecondaryDisplayError("OBS browser source support is not available on this system. The OBS secondary display is not compatible here.")
        input_response = self._request("GetInputList")
        input_names = {item.get("inputName") for item in input_response.get("inputs", []) if isinstance(item, dict)}
        browser_settings = self._build_browser_settings()
        browser_source_name = self._browser_source_name()
        scene_name = self._scene_name()
        if browser_source_name not in input_names:
            self._request(
                "CreateInput",
                {
                    "sceneName": scene_name,
                    "inputName": browser_source_name,
                    "inputKind": "browser_source",
                    "inputSettings": browser_settings,
                    "sceneItemEnabled": True,
                },
            )
        else:
            self._request(
                "SetInputSettings",
                {
                    "inputName": browser_source_name,
                    "inputSettings": browser_settings,
                    "overlay": True,
                },
            )
        self.browser_source_settings = dict(browser_settings)
        self.browser_source_ready = True
        try:
            self._request("PressInputPropertiesButton", {"inputName": browser_source_name, "propertyName": "refreshnocache"})
        except Exception:
            pass

    def _sync_browser_source_settings_if_needed(self) -> None:
        """What: Update the OBS browser input only when its settings have changed.

        Why included: Display bounds can change, but identical browser settings need no
        websocket request.
        Where used: push_state() before publishing renderer state.
        Returns: None; updates the cached settings after a successful OBS request.
        """
        browser_settings = self._build_browser_settings()
        if self.browser_source_settings == browser_settings:
            return
        self._request(
            "SetInputSettings",
            {
                "inputName": self._browser_source_name(),
                "inputSettings": browser_settings,
                "overlay": True,
            },
        )
        self.browser_source_settings = dict(browser_settings)

    def _build_browser_settings(self) -> dict:
        """What: Build browser-source URL and viewport settings for current display bounds.

        Why included: OBS needs the local server URL and exact target dimensions.
        Where used: Browser-source create, update, and change-detection paths.
        Returns: OBS browser input settings mapping.
        """
        display_config = self.get_display_config()
        display_bounds = display_config.get("display_bounds") or {}
        width = int(display_bounds.get("width") or 1920)
        height = int(display_bounds.get("height") or 1080)
        return {
            "url": f"http://127.0.0.1:{self.state_server.port}/secondary_display.html",
            "width": width,
            "height": height,
            "shutdown": False,
            "restart_when_active": False,
            "reroute_audio": False,
            "fps_custom": True,
            "fps": 60,
        }

    def _ensure_runtime(self) -> None:
        """What: Ensure both the OBS websocket client and browser source are ready.

        Why included: State pushes require a configured source and active OBS runtime.
        Where used: push_state() before state-server updates and projector opening, and warm_up().
        Returns: None; setup errors propagate as ObsSecondaryDisplayError.
        """
        self._ensure_client()
        self._ensure_browser_source()

    def warm_up(self) -> None:
        """What: Start and connect OBS ahead of time without showing anything on screen.

        Why included: OBS takes 15-35 seconds to start; doing it when the app opens means
        the first item appears without that wait.
        Where used: MirrorDisplayController.prewarm_secondary_output() at app startup.
        Returns: None; failures are logged and retried by the first real push.
        """
        try:
            self._ensure_runtime()
        except Exception as error:
            self.client = None
            log_exception("obs_secondary_display.warm_up", error)

    def _coerce_int(self, value: object) -> int | None:
        """What: Convert a value to int while treating common invalid values as absent.

        Why included: OBS monitor metadata may use different types or omit fields.
        Where used: Geometry parsing and monitor-index selection.
        Returns: Integer value, or None for TypeError/ValueError.
        """
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _get_bounds_geometry(self, bounds: dict | None) -> tuple[int, int, int, int] | None:
        """What: Parse x/y/width/height from the app's display-bounds mapping.

        Why included: Monitor matching requires complete integer geometry.
        Where used: _match_obs_monitor_index() when selecting the projector monitor.
        Returns: Geometry tuple, or None when fields are missing/invalid.
        """
        if not isinstance(bounds, dict):
            return None
        x = self._coerce_int(bounds.get("x"))
        y = self._coerce_int(bounds.get("y"))
        width = self._coerce_int(bounds.get("width"))
        height = self._coerce_int(bounds.get("height"))
        if None in {x, y, width, height}:
            return None
        return x, y, width, height

    def _get_obs_monitor_geometry(self, monitor: dict) -> tuple[int, int, int, int] | None:
        """What: Parse OBS monitor geometry across its alternate field-name formats.

        Why included: OBS versions may report monitorPositionX/Width or simpler x/width keys.
        Where used: _match_obs_monitor_index() for physical display matching.
        Returns: Geometry tuple, or None when any coordinate/dimension is unavailable.
        """
        if not isinstance(monitor, dict):
            return None
        x = self._coerce_int(monitor.get("monitorPositionX"))
        if x is None:
            x = self._coerce_int(monitor.get("x"))
        y = self._coerce_int(monitor.get("monitorPositionY"))
        if y is None:
            y = self._coerce_int(monitor.get("y"))
        width = self._coerce_int(monitor.get("monitorWidth"))
        if width is None:
            width = self._coerce_int(monitor.get("width"))
        height = self._coerce_int(monitor.get("monitorHeight"))
        if height is None:
            height = self._coerce_int(monitor.get("height"))
        if None in {x, y, width, height}:
            return None
        return x, y, width, height

    def _match_obs_monitor_index(self, monitors: list[dict], display_bounds: dict | None) -> int | None:
        """What: Find the OBS monitor index whose geometry matches app display bounds.

        Why included: The projector should open on the monitor selected by the app's
        secondary-display policy rather than a stale saved index.
        Where used: _select_monitor_index().
        Returns: Matching OBS index, or None when no geometry matches.
        """
        target_geometry = self._get_bounds_geometry(display_bounds)
        if target_geometry is None:
            return None
        for fallback_index, monitor in enumerate(monitors):
            monitor_index = self._coerce_int(monitor.get("monitorIndex"))
            if monitor_index is None:
                monitor_index = fallback_index
            if self._get_obs_monitor_geometry(monitor) == target_geometry:
                return monitor_index
        return None

    def _select_monitor_index(self) -> int:
        """What: Choose the OBS monitor matching current bounds, then saved/fallback index.

        Why included: Monitor indices can change as displays are connected or rearranged.
        Where used: _open_projector_if_needed() when opening the browser source projector.
        Returns: Selected OBS monitor index, defaulting to zero.
        """
        configured_index = self.settings.get("projector_monitor_index")
        configured_index_int = self._coerce_int(configured_index)
        display_bounds = self.get_display_config().get("display_bounds")
        try:
            monitor_response = self._request("GetMonitorList")
        except Exception:
            if configured_index_int is not None:
                return configured_index_int
            return 0
        monitors = monitor_response.get("monitors") or []
        if isinstance(monitors, list):
            matched_index = self._match_obs_monitor_index(monitors, display_bounds)
            if matched_index is not None:
                self.settings["projector_monitor_index"] = matched_index
                return matched_index
            valid_indices: set[int] = set()
            for fallback_index, monitor in enumerate(monitors):
                monitor_index = self._coerce_int(getattr(monitor, "get", lambda _key: None)("monitorIndex"))
                if monitor_index is None:
                    monitor_index = fallback_index
                valid_indices.add(monitor_index)
            if configured_index_int in valid_indices:
                return configured_index_int
            if valid_indices:
                return sorted(valid_indices)[0]
        if configured_index_int is not None:
            return configured_index_int
        return 0

    def _open_projector_if_needed(self) -> None:
        """What: Open the browser-source projector when visible and not already open.

        Why included: Repeated state pushes should not create duplicate projector windows.
        Where used: push_state() after a successful state update.
        Returns: None; marks the projector open after OBS accepts the request.
        """
        if self.projector_opened or self.current_window_mode == "hidden":
            return
        self._request(
            "OpenSourceProjector",
            {
                "sourceName": self._browser_source_name(),
                "monitorIndex": self._select_monitor_index(),
            },
        )
        self.projector_opened = True

    def _close_projector_windows(self) -> None:
        """What: Close visible Windows projector windows belonging to this browser source.

        Why included: Hiding/closing output should release the projector window without
        terminating the OBS application.
        Where used: set_visible(False) and controller close.
        Returns: None; non-Windows systems simply clear the open flag.
        """
        if os.name != "nt":
            self.projector_opened = False
            return
        source_name = self._browser_source_name().strip().lower()
        if not source_name:
            self.projector_opened = False
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_windows = user32.EnumWindows
        enum_windows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p), ctypes.c_void_p]
        enum_windows.restype = ctypes.c_bool
        get_window_text_length = user32.GetWindowTextLengthW
        get_window_text_length.argtypes = [ctypes.c_void_p]
        get_window_text_length.restype = ctypes.c_int
        get_window_text = user32.GetWindowTextW
        get_window_text.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        get_window_text.restype = ctypes.c_int
        is_window_visible = user32.IsWindowVisible
        is_window_visible.argtypes = [ctypes.c_void_p]
        is_window_visible.restype = ctypes.c_bool
        post_message = user32.PostMessageW
        post_message.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        post_message.restype = ctypes.c_bool

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_proc(hwnd, _lparam):
            """What: Inspect one top-level window and close matching OBS projectors.

            Why included: EnumWindows reports handles; title matching identifies the
            projector window for this configured source.
            Where used: Win32 EnumWindows callback in _close_projector_windows().
            Returns: True to continue enumerating all windows.
            """
            if not is_window_visible(hwnd):
                return True
            title_length = get_window_text_length(hwnd)
            if title_length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(title_length + 1)
            get_window_text(hwnd, buffer, len(buffer))
            title_text = buffer.value.strip().lower()
            if "projector" not in title_text:
                return True
            if source_name not in title_text:
                return True
            post_message(hwnd, WM_CLOSE, None, None)
            return True

        try:
            enum_windows(enum_proc, None)
        finally:
            self.projector_opened = False

    def _build_state(self) -> dict:
        """What: Fit media/overlay state to display bounds and render image bytes.

        Why included: OBS consumes one normalized state mapping plus an optional PNG overlay,
        while source type controls Bible, Welcome, Preparation, and scroll rendering.
        Where used: push_state(), set_hot_backgrounds(), and close().
        Returns: Pair of renderer state mapping and transparent overlay PNG bytes.
        """
        display_config = self.get_display_config()
        effective_window_mode = self.current_window_mode
        if effective_window_mode != "hidden":
            effective_window_mode = str(display_config.get("window_mode") or effective_window_mode)
        resolved_background_path = self.resolve_background_path(self.current_background_path)
        display_bounds = display_config.get("display_bounds") or {}
        render_width = max(1, int(display_bounds.get("width") or 1920))
        render_height = max(1, int(display_bounds.get("height") or 1080))
        fitted_background = None
        if Image is not None and ImageOps is not None and resolved_background_path and resolved_background_path.exists():
            try:
                if resolved_background_path.suffix.lower() in VIDEO_EXTENSIONS:
                    fitted_background = Image.new("RGB", (render_width, render_height), "#000000") if Image is not None else None
                else:
                    with Image.open(resolved_background_path) as source_image:
                        oriented_image = ImageOps.exif_transpose(source_image).convert("RGB")
                        fit_background = ImageOps.pad if oriented_image.width < oriented_image.height else ImageOps.fit
                        fitted_background = fit_background(
                            oriented_image,
                            (render_width, render_height),
                            method=Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
                        )
            except Exception as error:
                log_exception("obs_secondary_display._build_state", error, details=f"background={resolved_background_path}")
                fitted_background = None
        if fitted_background is not None and resolved_background_path:
            setattr(fitted_background, "_background_path", str(resolved_background_path))
        source_key = str(self.current_overlay_source or "").casefold()
        size_boost = WELCOME_RENDER_SIZE_BOOST if source_key == "welcome" else SECONDARY_RENDER_SIZE_BOOST
        line_spacing_ratio = 0.28
        if source_key == "preparation":
            size_boost = 1.0
            line_spacing_ratio = 0.14
        layout = fit_overlay_layout(
            self.current_overlay_text,
            scale_overlay_font_value(normalize_font_value(self.current_overlay_font), render_width, render_height),
            render_width,
            render_height,
            scroll_enabled=bool(self.current_overlay_scroll_enabled),
            base_image=fitted_background,
            background_path=resolved_background_path,
            line_spacing_ratio=line_spacing_ratio,
            size_boost=size_boost,
            corner_font_size=_get_secondary_corner_font_size(self.current_overlay_font, render_width, render_height),
        )
        section_layout = None
        sections = self.current_overlay_sections if isinstance(self.current_overlay_sections, dict) else None
        if source_key == "welcome" and not self.current_overlay_scroll_enabled and sections and sections.get("combine_enabled"):
            section_layout = fit_sectioned_overlay_layout(
                str(sections.get("centered_text") or ""),
                str(sections.get("centered_detail_text") or ""),
                str(sections.get("detail_text") or ""),
                scale_overlay_font_value(normalize_font_value(self.current_overlay_font), render_width, render_height),
                render_width,
                render_height,
                base_image=fitted_background,
                background_path=resolved_background_path,
                size_boost=size_boost,
            )
        overlay_image_png = b""
        bible_columns_present = source_key == "bible" and bool(self.current_overlay_columns)
        if bible_columns_present and not self.current_overlay_scroll_enabled:
            bible_overlay = render_bible_columns_overlay(
                render_width,
                render_height,
                self.current_overlay_columns,
                self.current_selected_verse_index,
                scale_overlay_font_value(normalize_font_value(self.current_overlay_font), render_width, render_height),
                fitted_background,
                background_path=resolved_background_path,
                stroke_width=1,
                size_boost=SECONDARY_RENDER_SIZE_BOOST,
                locked_font_value=get_bible_locked_font_value(self.current_overlay_columns, normalize_font_value(self.current_overlay_font), render_width, render_height, size_boost=SECONDARY_RENDER_SIZE_BOOST),
            )
            overlay_image_png = encode_png_bytes(bible_overlay)
        elif not self.current_overlay_scroll_enabled and build_sized_text_fields(source_key, self.current_overlay_text, self.current_overlay_font, sections):
            fields = build_sized_text_fields(source_key, self.current_overlay_text, self.current_overlay_font, sections)
            overlay_image_png = encode_png_bytes(render_text_fields_overlay(render_width, render_height, fields, base_image=fitted_background, background_path=resolved_background_path))
        elif not self.current_overlay_scroll_enabled:
            overlay_image = render_sectioned_overlay_image(render_width, render_height, section_layout) if section_layout is not None else render_overlay_image(
                render_width,
                render_height,
                layout,
                corner_text=self.current_overlay_corner_text,
            )
            overlay_image_png = encode_png_bytes(overlay_image)
        normalized_columns: list[dict[str, object]] = []
        for column in self.current_overlay_columns[:3]:
            if not isinstance(column, dict):
                continue
            verses = column.get("verses")
            if not isinstance(verses, list):
                continue
            normalized_columns.append({
                "title": str(column.get("title") or "Bible"),
                "verses": [str(value or "").strip() for value in verses if str(value or "").strip()],
            })
        return {
            "display_bounds": display_config.get("display_bounds"),
            "window_mode": effective_window_mode,
            "visible": effective_window_mode != "hidden",
            "background_path": str(resolved_background_path) if resolved_background_path else "",
            "overlay_text": self.current_overlay_text,
            "overlay_render_text": layout.text,
            "overlay_corner_text": self.current_overlay_corner_text,
            "overlay_corner_font_size": get_display_key_size(render_width, render_height),
            "overlay_corner_detail_font_size": get_corner_detail_size(get_display_key_size(render_width, render_height), render_width, render_height),
            "overlay_font": self.serialize_overlay_font(self.current_overlay_font),
            "overlay_render_font": self.serialize_overlay_font(layout.font_value),
            "overlay_outline_width": get_browser_outline_width(layout.font_value[1]),
            "overlay_foreground": self.current_overlay_foreground or layout.fill_color,
            "overlay_stroke_color": layout.stroke_color,
            "overlay_image_available": bool(overlay_image_png),
            "overlay_scroll_enabled": bool(self.current_overlay_scroll_enabled),
            "overlay_scroll_speed": self.current_overlay_scroll_speed,
            "overlay_scroll_origin": self.current_overlay_scroll_origin,
            "overlay_columns": normalized_columns,
            "selected_verse_index": int(self.current_selected_verse_index or 0),
            "transition_revision": int(self.current_transition_revision or 0),
            "hot_background_paths": list(self.current_hot_background_paths),
        }, overlay_image_png

    def push_state(self) -> None:
        """What: Publish changed renderer state, configure OBS, and open its projector.

        Why included: This is the main commit path from controller fields to browser output.
        Where used: render(), set_content(), and other state-change methods.
        Returns: None; skips identical signatures and records push timing.
        """
        started_at = time.perf_counter()
        next_signature = self._build_push_signature()
        if next_signature == self._last_push_signature:
            return
        next_state, overlay_image_png = self._build_state()
        self._ensure_runtime()
        self._sync_browser_source_settings_if_needed()
        self.state_server.update_state(next_state, overlay_image_png=overlay_image_png)
        self._last_push_signature = next_signature
        self._open_projector_if_needed()
        log_timing("obs.push_state", (time.perf_counter() - started_at) * 1000.0, threshold_ms=20.0, details=f"scroll={int(bool(next_state.get('overlay_scroll_enabled')))}")

    def state_poll_age(self) -> float:
        """What: Report seconds since the output page last polled renderer state.

        Why included: The mirror watchdog recovers output that stopped consuming state.
        Where used: Callback mapping returned by create_obs_secondary_mirror_window().
        Returns: Non-negative age in seconds from the loopback state server.
        """
        return self.state_server.state_poll_age()

    def is_alive(self) -> bool:
        """What: Report whether this controller has not been shut down.

        Why included: Mirror callers can avoid sending state to a closed runtime.
        Where used: Callback API returned by create_obs_secondary_mirror_window().
        Returns: True until close() marks the controller closed.
        """
        return not self.closed

    def render(self) -> None:
        """What: Rebuild and push the current state to OBS.

        Why included: Provides the callback-style refresh API expected by mirror.py.
        Where used: Controller callback mapping consumed by MirrorDisplayController.
        Returns: None; delegates all work to push_state().
        """
        self.push_state()

    def set_visible(self, visible: bool) -> None:
        """What: Set projector mode to the configured visible mode or hidden.

        Why included: External output visibility is independent of saved queue content.
        Where used: Secondary display toggle and push workflow.
        Returns: None; closes projector immediately when hiding and invalidates push signature.
        """
        next_window_mode = str(self.get_display_config().get("window_mode") or self.initial_window_mode) if visible else "hidden"
        if next_window_mode == self.current_window_mode:
            return
        if visible:
            self.current_window_mode = next_window_mode
            self._last_push_signature = None
            return
        else:
            self.current_window_mode = next_window_mode
            self._close_projector_windows()
        self._last_push_signature = None
        self.push_state()

    def set_background(self, background_path: Path) -> None:
        """What: Replace the currently selected background path in controller state.

        Why included: Mirror's callback API supports changing media independently of overlay text.
        Where used: Callback mapping returned to MirrorDisplayController.
        Returns: None; state is sent on a later render/push.
        """
        self.current_background_path = background_path

    def set_overlay(self, text: str, font_value=None, foreground=None, scroll_enabled: bool = False, scroll_speed: object = DEFAULT_SCROLL_SPEED, scroll_origin: float | None = None, corner_text: str | None = None, render_text: str | None = None, render_font=None, source: str | None = None, sections: dict[str, object] | None = None, transition_revision: int = 0, display_columns: list[dict[str, object]] | None = None, selected_verse_index: int = 0) -> None:
        """What: Replace text, typography, scrolling, source sections, and Bible selection state.

        Why included: Browser overlay rendering needs all source-specific text details in one
        normalized controller state.
        Where used: set_content() and the callback API exposed to mirror.py.
        Returns: None; invalidates the cached push signature.
        """
        self.current_overlay_text = text
        self.current_overlay_corner_text = str(corner_text or "")
        self.current_overlay_source = str(source or "")
        self.current_overlay_sections = sections if isinstance(sections, dict) else None
        self.current_overlay_columns = list(display_columns) if isinstance(display_columns, list) else []
        self.current_selected_verse_index = max(0, int(selected_verse_index or 0))
        self.current_overlay_font = font_value
        self.current_overlay_render_text = None
        self.current_overlay_render_font = None
        self.current_overlay_foreground = foreground
        self.current_overlay_scroll_enabled = bool(scroll_enabled)
        self.current_overlay_scroll_speed = normalize_scroll_speed_value(scroll_speed)
        self.current_overlay_scroll_origin = scroll_origin if scroll_origin is not None else self.get_scroll_timestamp()
        self.current_transition_revision = int(transition_revision or 0)
        self._last_push_signature = None

    def set_content(self, background_path: Path, text: str, font_value=None, foreground=None, scroll_enabled: bool = False, scroll_speed: object = DEFAULT_SCROLL_SPEED, scroll_origin: float | None = None, corner_text: str | None = None, render_text: str | None = None, render_font=None, source: str | None = None, sections: dict[str, object] | None = None, transition_revision: int = 0, display_columns: list[dict[str, object]] | None = None, selected_verse_index: int = 0) -> None:
        """What: Update background and overlay fields together, then publish to OBS.

        Why included: A new queue selection should transition its media and text as one state.
        Where used: MirrorDisplayController when forwarding a live payload.
        Returns: None; invokes push_state() after updating all fields.
        """
        self.current_background_path = background_path
        self.set_overlay(text, font_value, foreground, scroll_enabled, scroll_speed, scroll_origin, corner_text, render_text, render_font, source, sections, transition_revision, display_columns, selected_verse_index)
        self.push_state()

    def set_hot_backgrounds(self, background_paths: list[Path | str | None]) -> None:
        """What: Normalize, retain, and preload likely upcoming background files.

        Why included: Preloading reduces media switching delays in the browser renderer.
        Where used: MirrorDisplayController queue-change callback.
        Returns: None; publishes revised state when the hot-path list changes.
        """
        next_hot_background_paths = self.build_hot_background_paths(background_paths)
        if next_hot_background_paths == self.current_hot_background_paths:
            return
        self.current_hot_background_paths = next_hot_background_paths
        self.preload_backgrounds(background_paths)
        if not self.closed:
            self._last_push_signature = None
            next_state, overlay_image_png = self._build_state()
            self.state_server.update_state(next_state, overlay_image_png=overlay_image_png)

    def close(self) -> None:
        """What: Hide/close projector windows, publish hidden state, and stop the state server.

        Why included: OBS/browser-server resources must be released when output is disabled
        or the app shuts down.
        Where used: MirrorDisplayController.close() during application shutdown.
        Returns: None; safe to call more than once.
        """
        if self.closed:
            return
        self.closed = True
        try:
            self.current_window_mode = "hidden"
            self._close_projector_windows()
            self._last_push_signature = None
            next_state, overlay_image_png = self._build_state()
            self.state_server.update_state(next_state, overlay_image_png=overlay_image_png)
        except Exception:
            pass
        self.state_server.shutdown()
        self.client = None


def create_obs_secondary_mirror_window(
    app_base_path: Path,
    initial_window_mode: str,
    default_background_path: Path,
    get_display_config: Callable[[], dict[str, object]],
    resolve_background_path: Callable[[Path], Path],
    serialize_overlay_font: Callable[[object], dict | None],
    scroll_speed_options: tuple[str, ...],
    get_scroll_timestamp: Callable[[], float],
    build_hot_background_paths: Callable[[list[Path | str | None]], list[str]],
    preload_backgrounds: Callable[[list[Path | str | None]], None],
) -> dict[str, object]:
    """Create an OBS controller and expose the callback API used by mirror.py.

    What: Instantiates ObsSecondaryDisplayController and returns bound state, visibility,
    content, render, preload, and close operations.
    Why included: Mirror can integrate external output without depending on controller internals.
    Where used: MirrorDisplayController.ensure_secondary_controller().
    Returns: Mapping of controller callbacks and an is_alive status function.
    """
    controller = ObsSecondaryDisplayController(
        app_base_path=app_base_path,
        initial_window_mode=initial_window_mode,
        default_background_path=default_background_path,
        get_display_config=get_display_config,
        resolve_background_path=resolve_background_path,
        serialize_overlay_font=serialize_overlay_font,
        scroll_speed_options=scroll_speed_options,
        get_scroll_timestamp=get_scroll_timestamp,
        build_hot_background_paths=build_hot_background_paths,
        preload_backgrounds=preload_backgrounds,
    )
    return {
        "render": controller.render,
        "set_background": controller.set_background,
        "set_overlay": controller.set_overlay,
        "set_content": controller.set_content,
        "set_hot_backgrounds": controller.set_hot_backgrounds,
        "set_visible": controller.set_visible,
        "close": controller.close,
        "is_alive": controller.is_alive,
        "state_poll_age": controller.state_poll_age,
        "warm_up": controller.warm_up,
    }