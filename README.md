# EnMP banana

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Rhizobium-gits/EnMP-banana/blob/main/EnMP_banana.ipynb)

**EnMP banana** = **En**hance **M**y **P**laylist **banana**.

Spotify公開プレイリストのURLから、その中身（アルバム画像・ジャンル・タイトル・作成者）を集めて、
Gemini または ChatGPT(OpenAI) に **Instagramストーリー規格（1080×1920, 9:16）の縦サムネ画像** を作らせるツール。
右下にはSpotifyロゴと "Let's listen on Spotify!" のCTA、上部にプレイリスト名と作者名がレンダリングされる。

Spotify Developer Dashboardでアプリ登録して得られる Client ID / Secret を使い、
**Authorization Code Flow** で動く（2024年末のSpotify API仕様変更で、プレイリストの中身を
取るには Client Credentials だけではダメになったため）。初回だけブラウザでSpotifyにログインして
リダイレクト先URLをColabのプロンプトに貼る、それ以降は自動。

---

## できること

- 公開プレイリストURLを1本入れるだけで縦サムネが完成
- 背景アートは4パターンから選べる：
  - `collage` (無料・API不要): ジャケ写の色を抽出してジャケ写ぼかしブロブ＋角丸グリッドで合成
  - `pollinations` (無料・認証不要のAI): https://pollinations.ai のFLUX/SDXLバックエンドにテキストプロンプトでAIイラストを描かせる。生成30-120秒
  - `gemini` (`gemini-2.5-flash-image` = nano banana / `gemini-3-pro-image-preview` = pro): Gemini画像生成。**有料tier必須**（フリーtierは現状不可）
  - `openai` (`gpt-image-1`): ChatGPTと同じ画像生成。少額課金で利用可
- 文字とSpotifyロゴはPILで合成するのでブランド表記が崩れない

## 動かし方（Colab）

[`EnMP_banana.ipynb`](./EnMP_banana.ipynb) を Colab で開いて、上から実行するだけ。
必要なのは以下3つの鍵：

| 鍵 | 取り方 |
|---|---|
| Spotify Client ID / Secret | https://developer.spotify.com/dashboard で Create app → コピー。**Redirect URI には `http://127.0.0.1:8888/callback` を登録**しておく |
| Gemini API key | https://aistudio.google.com/apikey （無料tierでOK） |
| OpenAI API key | https://platform.openai.com/api-keys （`gpt-image-1` 使用時のみ） |

初回実行時、Spotipyが認証URLをセル下に表示するので：
1. URLをコピーしてブラウザで開く → Spotifyにログイン → "Agree"
2. リダイレクトされた404ページのURLバーを**まるごとコピー**
3. Colabの `Enter the URL you were redirected to:` プロンプトに貼り付けてEnter

これで `.spotify_cache` にtokenが保存され、以降は自動で再利用される。

`PROVIDER = "collage"` / `"pollinations"` / `"gemini"` / `"openai"` を切り替えるだけで生成エンジンが切り替わる。
無料で確実に動かしたいなら `"collage"`、無料でAIっぽいイラストが欲しいなら `"pollinations"`。

## ローカル（Python）で動かす

```bash
pip install -r requirements.txt
```

```python
from enmp_banana import make_thumbnail

img, meta = make_thumbnail(
    playlist_url="https://open.spotify.com/playlist/XXXXXXXXXXXX",
    spotify_client_id="...",
    spotify_client_secret="...",
    spotify_redirect_uri="http://127.0.0.1:8888/callback",
    provider="gemini",                    # or "openai"
    gemini_api_key="...",
    # openai_api_key="...",
    output_path="playlist_thumbnail.png",
)
print(meta.name, "by", meta.owner, "/ genres:", meta.top_genres)
```

## 仕組み

1. `spotipy` で Authorization Code Flow 認証 → プレイリストの全トラックをページング取得
2. アーティスト endpoint からジャンルを集めて多数決でtop genres決定
3. アルバム画像を最大6枚ダウンロード
4. 背景アート生成
   - Gemini: アルバム画像を入力として渡しつつ「文字は描くな・上中央と右下は空ける」と指示
   - OpenAI: `images.edit` にアルバム画像を入力として渡して同様のプロンプト
5. PIL で 1080×1920 にリサイズ → 上下にうっすら黒グラデ → タイトル / 作者 / ジャンル / Spotifyロゴ + "Let's listen on Spotify!" を合成

## モデル選択ガイド

| 用途 | 推奨 |
|---|---|
| 無料・確実に動かしたい | `PROVIDER="collage"` (API不要のローカル合成) |
| 無料でAI生成イラストが欲しい | `PROVIDER="pollinations"` (認証不要のFLUX) |
| 画質と質感重視 | `gemini-3-pro-image-preview` (nano banana pro, 有料) |
| ChatGPTで揃えたい | `gpt-image-1` (OpenAI, 少額課金) |

## ライセンス

MIT
