"""EnMP banana - Enhance Playlist Banana.

Spotifyの公開プレイリストURLから、アルバム画像とジャンルを集めて、
GeminiまたはChatGPT(OpenAI)に縦長サムネ(1080x1920)を作らせるツール。
"""
from __future__ import annotations

import base64
import io
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests
import spotipy
from PIL import Image, ImageDraw, ImageFont
from spotipy.oauth2 import SpotifyClientCredentials

CANVAS_W, CANVAS_H = 1080, 1920
SPOTIFY_GREEN = (30, 215, 96)
SPOTIFY_LOGO_URL = (
    "https://storage.googleapis.com/pr-newsroom-wp/1/2018/11/"
    "Spotify_Logo_RGB_White.png"
)


# 🐱 Spotifyから集めたメタ情報の入れ物
@dataclass
class PlaylistMeta:
    playlist_id: str
    name: str
    owner: str
    top_genres: List[str]
    cover_urls: List[str]
    cover_images: List[Image.Image]
    track_count: int


def extract_playlist_id(url_or_id: str) -> str:
    """SpotifyプレイリストURL/URI/IDから生IDを取り出す."""
    m = re.search(r"playlist[/:]([a-zA-Z0-9]+)", url_or_id)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]+", url_or_id):
        return url_or_id
    raise ValueError(f"Could not parse playlist id from: {url_or_id}")


def fetch_playlist_meta(
    playlist_url: str,
    spotify_client_id: str,
    spotify_client_secret: str,
    max_covers: int = 6,
) -> PlaylistMeta:
    """Client Credentials認証でSpotifyからメタ情報をまとめて取得する."""
    sp = spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=spotify_client_id,
            client_secret=spotify_client_secret,
        )
    )
    playlist_id = extract_playlist_id(playlist_url)

    # 🐱 基本情報だけ取る(tracksは別エンドポイントで)
    basic = sp.playlist(playlist_id, fields="name,owner(display_name)")
    if not isinstance(basic, dict) or "name" not in basic:
        raise RuntimeError(
            "Spotify did not return playlist metadata. "
            "Check that the playlist URL is correct and the playlist is PUBLIC, "
            "and that Client ID/Secret are valid. "
            f"Got: {basic!r}"
        )
    playlist_name = basic["name"]
    owner_name = (basic.get("owner") or {}).get("display_name") or "Unknown"

    # 🐱 トラック一覧はplaylist_itemsで明示的にページング
    items: List[dict] = []
    page = sp.playlist_items(
        playlist_id,
        limit=100,
        additional_types=("track",),
    )
    while page is not None:
        items.extend(page.get("items") or [])
        if page.get("next"):
            page = sp.next(page)
        else:
            break

    artist_ids: List[str] = []
    cover_urls: List[str] = []
    for it in items:
        track = it.get("track") or {}
        for a in track.get("artists", []):
            if a.get("id"):
                artist_ids.append(a["id"])
        images = (track.get("album") or {}).get("images") or []
        if images:
            cover_urls.append(images[0]["url"])

    # 🐱 ジャンルはアーティスト経由で集めて多数決
    genres: List[str] = []
    uniq_artists = list(dict.fromkeys(artist_ids))
    for i in range(0, len(uniq_artists), 50):
        chunk = uniq_artists[i : i + 50]
        if not chunk:
            continue
        for a in sp.artists(chunk)["artists"]:
            genres.extend(a.get("genres", []))
    top_genres = [g for g, _ in Counter(genres).most_common(5)] or ["mixed"]

    uniq_covers = list(dict.fromkeys(cover_urls))[:max_covers]
    cover_images: List[Image.Image] = []
    for u in uniq_covers:
        try:
            r = requests.get(u, timeout=20)
            r.raise_for_status()
            cover_images.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
        except Exception:
            continue

    return PlaylistMeta(
        playlist_id=playlist_id,
        name=playlist_name,
        owner=owner_name,
        top_genres=top_genres,
        cover_urls=uniq_covers,
        cover_images=cover_images,
        track_count=len(items),
    )


def _background_prompt(meta: PlaylistMeta) -> str:
    return (
        "Create a vertical 9:16 background artwork (Instagram Story size, 1080x1920) "
        "for a Spotify playlist moodboard.\n"
        f"Mood / genres: {', '.join(meta.top_genres)}.\n"
        "Style: stylish moodboard collage inspired by the provided album cover "
        "images, cohesive color palette matching the genre vibe, subtle film grain.\n"
        "Composition rules: leave clean negative space in the UPPER THIRD for a "
        "title overlay, and keep the BOTTOM RIGHT corner relatively clean for a "
        "logo overlay.\n"
        "STRICT: Do NOT render any text, letters, numbers, or logos. "
        "Background art only — text and logos are added later by the renderer."
    )


# ---------- Gemini provider ----------
def generate_background_gemini(
    meta: PlaylistMeta,
    api_key: str,
    model: str = "gemini-2.5-flash-image",
) -> Image.Image:
    """Gemini (nano banana / nano banana pro) で背景アートを生成."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    prompt = _background_prompt(meta)
    contents = [prompt, *meta.cover_images]
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    for part in resp.candidates[0].content.parts:
        inline = getattr(part, "inline_data", None)
        if inline and inline.data:
            return Image.open(io.BytesIO(inline.data)).convert("RGB")
    raise RuntimeError("Gemini response did not contain an image part")


# ---------- OpenAI provider ----------
def generate_background_openai(
    meta: PlaylistMeta,
    api_key: str,
    model: str = "gpt-image-1",
) -> Image.Image:
    """OpenAI (gpt-image-1) で背景アートを生成. アルバム画像を入力として使う."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    prompt = _background_prompt(meta)

    tmp_paths: List[str] = []
    try:
        for idx, img in enumerate(meta.cover_images):
            path = os.path.join(
                tempfile.gettempdir(), f"enmp_cover_{idx}.png"
            )
            img.save(path, format="PNG")
            tmp_paths.append(path)

        if tmp_paths:
            files = [open(p, "rb") for p in tmp_paths]
            try:
                resp = client.images.edit(
                    model=model,
                    image=files,
                    prompt=prompt,
                    size="1024x1536",
                )
            finally:
                for f in files:
                    f.close()
        else:
            resp = client.images.generate(
                model=model,
                prompt=prompt,
                size="1024x1536",
            )
    finally:
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    b64 = resp.data[0].b64_json
    if not b64:
        raise RuntimeError("OpenAI response did not contain b64 image data")
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


# ---------- Compositor ----------
def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc" if bold else
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap_text(
    text: str, font: ImageFont.FreeTypeFont, max_width: int,
    draw: ImageDraw.ImageDraw,
) -> List[str]:
    """日本語/英語どっちもいけるグリーディ折り返し."""
    if not text:
        return [""]
    lines: List[str] = []
    current = ""
    for ch in text:
        trial = current + ch
        w = draw.textlength(trial, font=font)
        if w <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def _fetch_spotify_logo() -> Optional[Image.Image]:
    try:
        r = requests.get(SPOTIFY_LOGO_URL, timeout=20)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGBA")
    except Exception:
        return None


def compose_thumbnail(
    background: Image.Image,
    meta: PlaylistMeta,
    cta_text: str = "Let's listen on Spotify!",
) -> Image.Image:
    """背景アートにタイトル/作者/Spotifyロゴ+CTAを重ねて1080x1920に仕上げる."""
    bg = background.convert("RGB").resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

    # 🐱 上部と下部に黒グラデで文字を浮かせる
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for y in range(CANVAS_H):
        top_a = int(140 * max(0.0, 1 - y / (CANVAS_H * 0.35)))
        bot_a = int(190 * max(0.0, (y - CANVAS_H * 0.55) / (CANVAS_H * 0.45)))
        a = min(220, top_a + bot_a)
        if a:
            od.line([(0, y), (CANVAS_W, y)], fill=(0, 0, 0, a))
    canvas = Image.alpha_composite(bg.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas)

    title_font = _load_font(96, bold=True)
    sub_font = _load_font(44, bold=False)
    genre_font = _load_font(34, bold=False)
    cta_font = _load_font(40, bold=True)

    margin = 70

    # 🐱 タイトル(自動折り返し、最大3行)
    title_lines = _wrap_text(meta.name, title_font, CANVAS_W - margin * 2, draw)[:3]
    y = 150
    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill="white")
        y += int(title_font.size * 1.15)

    # 🐱 作者
    draw.text(
        (margin, y + 10),
        f"by {meta.owner}",
        font=sub_font,
        fill=(235, 235, 235),
    )

    # 🐱 ジャンル(あれば)
    if meta.top_genres:
        genre_str = " · ".join(meta.top_genres[:3])
        draw.text(
            (margin, y + 10 + int(sub_font.size * 1.4)),
            genre_str.upper(),
            font=genre_font,
            fill=SPOTIFY_GREEN,
        )

    # 🐱 右下にCTA + Spotifyロゴ
    logo = _fetch_spotify_logo()
    cta_y = CANVAS_H - 130
    cta_w = draw.textlength(cta_text, font=cta_font)
    cta_x = CANVAS_W - margin - cta_w
    draw.text((cta_x, cta_y), cta_text, font=cta_font, fill="white")
    if logo is not None:
        logo.thumbnail((200, 200))
        canvas.alpha_composite(
            logo,
            (
                CANVAS_W - margin - logo.size[0],
                cta_y - logo.size[1] - 24,
            ),
        )

    return canvas.convert("RGB")


# ---------- High-level API ----------
def make_thumbnail(
    playlist_url: str,
    spotify_client_id: str,
    spotify_client_secret: str,
    provider: str = "gemini",
    gemini_api_key: Optional[str] = None,
    gemini_model: str = "gemini-2.5-flash-image",
    openai_api_key: Optional[str] = None,
    openai_model: str = "gpt-image-1",
    output_path: Optional[str] = "playlist_thumbnail.png",
    cta_text: str = "Let's listen on Spotify!",
) -> Tuple[Image.Image, PlaylistMeta]:
    """エンドツーエンドでサムネを作って(image, meta)を返す."""
    meta = fetch_playlist_meta(
        playlist_url, spotify_client_id, spotify_client_secret
    )

    if provider == "gemini":
        if not gemini_api_key:
            raise ValueError("gemini_api_key is required for provider='gemini'")
        bg = generate_background_gemini(meta, gemini_api_key, gemini_model)
    elif provider == "openai":
        if not openai_api_key:
            raise ValueError("openai_api_key is required for provider='openai'")
        bg = generate_background_openai(meta, openai_api_key, openai_model)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    img = compose_thumbnail(bg, meta, cta_text=cta_text)
    if output_path:
        img.save(output_path)
    return img, meta
