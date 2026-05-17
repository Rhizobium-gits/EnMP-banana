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
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from spotipy.oauth2 import SpotifyOAuth

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
    top_artists: List[str]
    top_tracks: List[str]
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
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback",
    max_covers: int = 6,
    cache_path: str = ".spotify_cache",
) -> PlaylistMeta:
    """Authorization Code Flow認証でSpotifyからメタ情報をまとめて取得する.

    初回のみ表示されるURLをブラウザで開いてSpotifyログイン → リダイレクト先URLを
    プロンプトに貼り付けるとtokenがcache_pathに保存され、以降は自動で再利用される.
    """
    sp = spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=spotify_client_id,
            client_secret=spotify_client_secret,
            redirect_uri=spotify_redirect_uri,
            scope="playlist-read-private",
            cache_path=cache_path,
            open_browser=False,
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
        market="from_token",
        additional_types=("track",),
    )
    while page is not None:
        items.extend(page.get("items") or [])
        if page.get("next"):
            page = sp.next(page)
        else:
            break

    artist_ids: List[str] = []
    artist_name_counter: Counter = Counter()
    track_names: List[str] = []
    cover_urls: List[str] = []
    n_null_track = n_local = n_no_image = 0
    for it in items:
        if it.get("is_local"):
            n_local += 1
        track = it.get("track")
        if not track:
            n_null_track += 1
            continue
        if track.get("name"):
            track_names.append(track["name"])
        for a in track.get("artists", []) or []:
            if a.get("id"):
                artist_ids.append(a["id"])
            if a.get("name"):
                artist_name_counter[a["name"]] += 1
        images = (track.get("album") or {}).get("images") or []
        if images:
            cover_urls.append(images[0]["url"])
        else:
            n_no_image += 1

    top_artists = [n for n, _ in artist_name_counter.most_common(5)]
    top_tracks = track_names[:5]

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

    print(
        f"[EnMP] '{playlist_name}' by {owner_name} | "
        f"tracks: {len(items)} (null:{n_null_track} local:{n_local} no_image:{n_no_image}) | "
        f"cover URLs: {len(uniq_covers)} | covers downloaded: {len(cover_images)} | "
        f"top genres: {top_genres}"
    )

    return PlaylistMeta(
        playlist_id=playlist_id,
        name=playlist_name,
        owner=owner_name,
        top_genres=top_genres,
        top_artists=top_artists,
        top_tracks=top_tracks,
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


# ---------- Local collage provider (no API, always works) ----------
# 🐱 ジャンル→(主色, 副色)のパレット。部分一致で拾うので "japanese pop" → "pop"などもヒット
GENRE_PALETTES: List[Tuple[str, Tuple[int, int, int], Tuple[int, int, int]]] = [
    ("metal",     (40, 40, 40),    (180, 30, 40)),
    ("punk",      (220, 40, 90),   (30, 30, 30)),
    ("rock",      (180, 50, 60),   (40, 40, 60)),
    ("trap",      (180, 100, 220), (40, 40, 60)),
    ("hip hop",   (220, 170, 60),  (90, 30, 110)),
    ("rap",       (220, 170, 60),  (40, 40, 40)),
    ("r&b",       (180, 80, 140),  (60, 30, 90)),
    ("soul",      (200, 130, 60),  (90, 40, 60)),
    ("jazz",      (180, 140, 80),  (40, 60, 90)),
    ("blues",     (40, 80, 140),   (180, 130, 60)),
    ("k-pop",     (255, 120, 180), (180, 100, 255)),
    ("j-pop",     (255, 130, 180), (130, 200, 255)),
    ("anime",     (255, 150, 200), (130, 180, 255)),
    ("city pop",  (255, 150, 120), (90, 180, 220)),
    ("pop",       (255, 100, 180), (90, 180, 255)),
    ("techno",    (30, 200, 220),  (180, 60, 255)),
    ("house",     (255, 200, 60),  (60, 100, 255)),
    ("edm",       (255, 60, 180),  (60, 220, 255)),
    ("electro",   (60, 220, 200),  (255, 70, 200)),
    ("dance",     (255, 70, 200),  (60, 220, 200)),
    ("drum and bass", (80, 220, 180), (180, 60, 220)),
    ("dnb",       (80, 220, 180),  (180, 60, 220)),
    ("dubstep",   (140, 50, 220),  (50, 200, 180)),
    ("indie",     (220, 180, 130), (90, 130, 100)),
    ("folk",      (180, 150, 100), (90, 110, 70)),
    ("country",   (200, 140, 80),  (90, 110, 130)),
    ("classical", (230, 215, 170), (40, 60, 100)),
    ("ambient",   (150, 180, 220), (200, 180, 230)),
    ("chill",     (180, 200, 230), (220, 200, 180)),
    ("lo-fi",     (220, 180, 200), (140, 130, 180)),
    ("lofi",      (220, 180, 200), (140, 130, 180)),
    ("latin",     (255, 140, 90),  (220, 60, 100)),
    ("reggae",    (220, 200, 60),  (60, 180, 100)),
    ("funk",      (255, 180, 60),  (220, 60, 180)),
    ("disco",     (255, 90, 200),  (255, 200, 60)),
]


def _palette_from_genres(genres: List[str]) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """top_genresからジャンル名の部分一致で(primary, secondary)を返す(ジャケ写ゼロ時のフォールバック用)."""
    for g in genres:
        gl = g.lower()
        for key, primary, secondary in GENRE_PALETTES:
            if key in gl:
                return primary, secondary
    return (140, 100, 220), (60, 200, 200)


def _extract_dominant_colors(
    covers: List[Image.Image], n: int = 4
) -> List[Tuple[int, int, int]]:
    """全ジャケ写から鮮やかな主要色を集約してN色返す.

    各画像をquantizeで色量子化→ニアグレー/ニア黒白を除外→
    彩度×出現量で重み付けして降順ソート、似た色はマージ.
    """
    weighted: List[Tuple[Tuple[int, int, int], int]] = []
    for cov in covers:
        small = cov.convert("RGB").resize((96, 96), Image.LANCZOS)
        q = small.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        counts = q.getcolors() or []
        for cnt, idx in counts:
            r, g, b = pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2]
            mx, mn = max(r, g, b), min(r, g, b)
            sat = mx - mn
            bright = (r + g + b) // 3
            # 🐱 暗すぎ/明るすぎ/グレーすぎは捨てる
            if bright < 35 or bright > 235 or sat < 35:
                continue
            weighted.append(((r, g, b), cnt * (sat + 30)))

    if not weighted:
        return []

    # 🐱 似た色をマージ(色差32以内なら同じグループ)
    weighted.sort(key=lambda x: -x[1])
    merged: List[Tuple[Tuple[int, int, int], int]] = []
    for color, w in weighted:
        placed = False
        for i, (mc, mw) in enumerate(merged):
            if (
                abs(color[0] - mc[0]) < 32
                and abs(color[1] - mc[1]) < 32
                and abs(color[2] - mc[2]) < 32
            ):
                merged[i] = (mc, mw + w)
                placed = True
                break
        if not placed:
            merged.append((color, w))

    merged.sort(key=lambda x: -x[1])
    return [c for c, _ in merged[:n]]


def _smart_palette(
    meta: PlaylistMeta,
) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """まずジャケ写から色抽出を試み、取れなければジャンル辞書にフォールバック."""
    if meta.cover_images:
        colors = _extract_dominant_colors(meta.cover_images, n=3)
        if len(colors) >= 2:
            return colors[0], colors[1]
        if len(colors) == 1:
            _, fallback_secondary = _palette_from_genres(meta.top_genres)
            return colors[0], fallback_secondary
    return _palette_from_genres(meta.top_genres)


def _blurred_cover_blob(
    cov: Image.Image, size: int, blur: int = 90
) -> Image.Image:
    """ジャケ写を超ぼかし+ソフト楕円マスクして装飾ブロブにする."""
    img = cov.convert("RGB").resize((size, size), Image.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(blur))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([(0, 0), (size, size)], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(40))
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def generate_background_collage(meta: PlaylistMeta) -> Image.Image:
    """ジャケ写の色とコンテンツを混ぜ合わせた1080x1920背景を作る.

    層構成:
      1. ジャケ写[0]を全画面ぼかしてベース (無ければ単色)
      2. ジャケ写から抽出した主色のウォッシュ
      3. ジャケ写[1..3]を超ぼかし+楕円マスクした「ブロブ」を四隅に配置(装飾)
      4. 中央に最大4枚のジャケ写を角丸+影付きで visible に配置(moodboard本体)
    """
    primary, secondary = _smart_palette(meta)
    covers = meta.cover_images

    # 1. ベース層: ジャケ写[0]を強くぼかしてフルキャンバスに
    if covers:
        canvas = covers[0].convert("RGB").resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
        canvas = canvas.filter(ImageFilter.GaussianBlur(80))
    else:
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), primary)
    canvas = canvas.convert("RGBA")

    # 2. ジャケ写抽出色のウォッシュ(彩度を保ちつつ画面を整える)
    wash = Image.new("RGBA", canvas.size, (*primary, 110))
    canvas = Image.alpha_composite(canvas, wash)

    # 3. ジャケ写そのものを使った装飾ブロブ(色は本物の写真の色)
    blob_specs = [
        (1, 900, (-180, -280)),
        (2, 900, (640, 1280)),
        (3, 700, (-80, 1520)),
        (0, 600, (780, 280)),
    ]
    for cov_idx, size, pos in blob_specs:
        if cov_idx < len(covers):
            blob = _blurred_cover_blob(covers[cov_idx], size, blur=100)
            canvas.alpha_composite(blob, pos)

    # 4. 単色ブロブを副色で1個だけ重ねて統一感を出す
    accent = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(accent).ellipse(
        [(300, 1400), (1100, 2200)], fill=(*secondary, 90)
    )
    accent = accent.filter(ImageFilter.GaussianBlur(120))
    canvas = Image.alpha_composite(canvas, accent)

    # 4. 中央のジャケ写moodboard(角丸)
    if covers:
        n = min(len(covers), 4)
        if n >= 4:
            ts = 340
            gap = 20
            grid_w = ts * 2 + gap
            grid_h = ts * 2 + gap
            start_x = (CANVAS_W - grid_w) // 2
            start_y = 880
            positions = [
                (start_x, start_y),
                (start_x + ts + gap, start_y),
                (start_x, start_y + ts + gap),
                (start_x + ts + gap, start_y + ts + gap),
            ]
        elif n == 3:
            ts = 300
            gap = 18
            start_x = (CANVAS_W - ts * 3 - gap * 2) // 2
            start_y = 1000
            positions = [(start_x + i * (ts + gap), start_y) for i in range(3)]
        elif n == 2:
            ts = 420
            gap = 24
            start_x = (CANVAS_W - ts * 2 - gap) // 2
            start_y = 950
            positions = [(start_x, start_y), (start_x + ts + gap, start_y)]
        else:
            ts = 560
            start_x = (CANVAS_W - ts) // 2
            start_y = 880
            positions = [(start_x, start_y)]

        mask = Image.new("L", (ts, ts), 0)
        ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (ts, ts)], radius=28, fill=255)

        tile_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        for i in range(n):
            cov = covers[i].resize((ts, ts), Image.LANCZOS).convert("RGBA")
            cov.putalpha(mask)
            # 🐱 影
            shadow = Image.new("RGBA", (ts + 40, ts + 40), (0, 0, 0, 0))
            sd_mask = Image.new("L", shadow.size, 0)
            ImageDraw.Draw(sd_mask).rounded_rectangle(
                [(20, 20), (20 + ts, 20 + ts)], radius=28, fill=140
            )
            sd_mask = sd_mask.filter(ImageFilter.GaussianBlur(16))
            shadow.putalpha(sd_mask)
            sx, sy = positions[i]
            tile_layer.alpha_composite(shadow, (sx - 20 + 6, sy - 20 + 12))
            tile_layer.alpha_composite(cov, (sx, sy))
        canvas = Image.alpha_composite(canvas, tile_layer)

    return canvas.convert("RGB")


# ---------- Pollinations provider (free AI, no auth) ----------
# 🐱 ジャンル→(具体イラスト指示, Pollinationsモデル)。部分一致でヒット
STYLE_TEMPLATES: List[Tuple[str, str, str]] = [
    ("metal",      "dark fantasy concert poster art, dramatic chiaroscuro lighting, gothic textures, brutalist composition, smoke and embers", "flux"),
    ("punk",       "distressed punk zine collage, torn paper, photocopy texture, DIY aesthetic, safety pins and stickers", "flux"),
    ("rock",       "vintage rock concert poster illustration, electric guitar imagery, warm sepia tones, film grain, halftone print", "flux"),
    ("jazz",       "smoky jazz club illustration, art deco style, sepia and amber tones, vinyl records, saxophone silhouettes, 1950s nightlife", "flux"),
    ("blues",      "moody bluesman silhouette, dimly lit bar, deep blue and warm amber, slide guitar imagery, painterly", "flux"),
    ("hip hop",    "urban graffiti street art mural, gritty city night, spray paint textures, sneakers and boomboxes, bold colors", "flux"),
    ("rap",        "urban graffiti street art mural, gritty city night, spray paint textures, sneakers and boomboxes, bold colors", "flux"),
    ("trap",       "dark luxurious street aesthetic, gold chains, smoke, urban night, neon signs reflection, cinematic", "flux"),
    ("r&b",        "soft sensual illustration, velvet curtains, candlelight, romantic mood, deep purples and burgundy, sultry", "flux"),
    ("soul",       "vintage soul record cover, retro 70s aesthetic, warm earth tones, microphone and stage lights, painterly", "flux"),
    ("classical",  "renaissance baroque painting style, ornate gold details, marble, soft chiaroscuro, oil painting", "flux"),
    ("ambient",    "ethereal cosmic landscape, nebula, dreamlike floating shapes, soft pastel gradients, abstract space art", "flux"),
    ("chill",      "calm pastel anime cityscape at sunset, lofi study aesthetic, soft lighting, cozy atmosphere", "flux-anime"),
    ("lo-fi",      "anime girl with headphones studying by window, rainy city night, cozy bedroom plants, lofi hiphop aesthetic", "flux-anime"),
    ("lofi",       "anime girl with headphones studying by window, rainy city night, cozy bedroom plants, lofi hiphop aesthetic", "flux-anime"),
    ("anime",      "anime illustration style, vibrant cel shading, expressive character art, soft pastel highlights", "flux-anime"),
    ("j-pop",      "anime style pop poster, sakura petals, bright pop colors, kawaii character, idol aesthetic", "flux-anime"),
    ("k-pop",      "modern pop idol poster, glossy futuristic fashion, bold color blocking, magazine editorial style", "flux"),
    ("city pop",   "1980s Japanese city pop aesthetic, neon Tokyo skyline at night, vaporwave palette, retro convertible car", "flux"),
    ("vaporwave",  "vaporwave aesthetic, Greek statue head, pink and cyan, retro 80s computer graphics, palm trees", "flux"),
    ("synthwave",  "synthwave neon grid, retro 1980s sunset, palm trees silhouettes, magenta and cyan, chrome", "flux"),
    ("techno",     "industrial warehouse rave, strobe lights, geometric patterns, dark concrete, smoke", "flux"),
    ("house",      "Ibiza beach club at sunset, palm trees, warm gradient sky, dance floor reflections", "flux"),
    ("edm",        "festival neon laser show, energetic crowd silhouettes, vivid stage lights, confetti, high energy", "flux"),
    ("electronic", "cyberpunk neon cityscape, futuristic, glowing wires, holographic projections, rain reflections", "flux"),
    ("dnb",        "abstract motion blur energy, geometric fractal patterns, neon green and purple, fast frenetic", "flux"),
    ("drum and bass", "abstract motion blur energy, geometric fractal patterns, neon green and purple, fast frenetic", "flux"),
    ("dubstep",    "glitch art aesthetic, fractured neon shapes, digital distortion, bass wave visualizations", "flux"),
    ("country",    "American western prairie, golden hour, vintage pickup truck, denim and wheat tones, big sky", "flux"),
    ("folk",       "watercolor forest illustration, hand-drawn, earthy tones, acoustic guitar by campfire, cozy", "flux"),
    ("indie",      "indie album cover illustration, dreamy washed-out colors, polaroid feel, melancholic mood", "flux"),
    ("latin",      "tropical sunset, palm trees, vibrant warm colors, salsa dancers silhouettes, festive", "flux"),
    ("reggae",     "rastafarian colors green yellow red, Caribbean beach, palm trees, mellow chill vibe", "flux"),
    ("disco",      "disco ball reflections, glittery 1970s dance floor, sparkles, bell bottoms silhouettes", "flux"),
    ("funk",       "70s funk poster, afro silhouettes, bell bottoms, psychedelic spiral patterns, warm orange and brown", "flux"),
    ("dance",      "energetic dance club, motion blur lights, vivid colors, silhouettes mid-movement", "flux"),
    ("pop",        "modern pop art illustration, bright bold colors, comic-book aesthetic, halftone dots", "flux"),
]


def _style_from_genres(genres: List[str]) -> Tuple[str, str]:
    """top_genres から具体的なスタイル指示とPollinationsモデルを決定."""
    for g in genres:
        gl = g.lower()
        for key, style, model in STYLE_TEMPLATES:
            if key in gl:
                return style, model
    return (
        "eclectic mixed-media moodboard, varied textures, surreal collage, painterly, "
        "rich color palette",
        "flux",
    )


def generate_background_pollinations(
    meta: PlaylistMeta,
    timeout: int = 180,
) -> Image.Image:
    """無料のpollinations.ai (FLUX) でジャンル別スタイルの1080x1920背景を生成.

    プロンプトを「ジャンル別の具体的なイラスト指示 + 実際のアーティスト名 +
    ジャケ写から抽出した色」で組み立てるので、プレイリストごとに絵柄が変わる.
    """
    import urllib.parse

    style_desc, model = _style_from_genres(meta.top_genres)

    color_hex: List[str] = []
    if meta.cover_images:
        for r, g, b in _extract_dominant_colors(meta.cover_images, n=4):
            color_hex.append(f"#{r:02x}{g:02x}{b:02x}")

    parts: List[str] = [
        f"Album cover style illustration inspired by the music playlist '{meta.name}'.",
        f"Visual direction: {style_desc}.",
    ]
    if meta.top_artists:
        parts.append("Inspired by artists like " + ", ".join(meta.top_artists[:3]) + ".")
    if meta.top_genres:
        parts.append("Music genres: " + ", ".join(meta.top_genres[:4]) + ".")
    if color_hex:
        parts.append("Color palette to follow: " + ", ".join(color_hex) + ".")
    parts.append(
        "Vertical portrait 9:16 composition. Leave clean negative space in the "
        "upper third for a title overlay and bottom right for a logo overlay. "
        "Strictly no text, no letters, no numbers, no logos, no watermarks."
    )
    prompt = " ".join(parts)

    # 🐱 同じプレイリストでも変化を出すためにseedを名前から決定論的に
    seed = abs(hash(meta.playlist_id + meta.name)) % 999999

    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(prompt, safe="")
        + f"?width=1080&height=1920&model={urllib.parse.quote(model)}"
        + f"&seed={seed}&nologo=true&enhance=true&private=true"
    )

    print(f"[EnMP] Pollinations style='{style_desc[:60]}...' model={model} seed={seed}")
    print(f"[EnMP] Generating (30-120s)...")
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


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
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback",
    provider: str = "gemini",
    gemini_api_key: Optional[str] = None,
    gemini_model: str = "gemini-2.5-flash-image",
    openai_api_key: Optional[str] = None,
    openai_model: str = "gpt-image-1",
    output_path: Optional[str] = "playlist_thumbnail.png",
    cta_text: str = "Let's listen on Spotify!",
    cache_path: str = ".spotify_cache",
) -> Tuple[Image.Image, PlaylistMeta]:
    """エンドツーエンドでサムネを作って(image, meta)を返す."""
    meta = fetch_playlist_meta(
        playlist_url,
        spotify_client_id,
        spotify_client_secret,
        spotify_redirect_uri=spotify_redirect_uri,
        cache_path=cache_path,
    )

    if provider == "collage":
        bg = generate_background_collage(meta)
    elif provider == "pollinations":
        bg = generate_background_pollinations(meta)
    elif provider == "gemini":
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
    print(
        f"[EnMP] provider={provider} | output size: {img.size[0]}x{img.size[1]} "
        f"(target 1080x1920, 9:16)"
        + (f" | saved: {output_path}" if output_path else "")
    )
    return img, meta
