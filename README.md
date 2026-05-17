# EnMP banana

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Rhizobium-gits/EnMP-banana/blob/main/EnMP_banana.ipynb)

**EnMP banana** = **En**hance **M**y **P**laylist **banana**.

Spotify公開プレイリストのURLから、その中身（アルバム画像・ジャンル・タイトル・作成者）を集めて、
Gemini または ChatGPT(OpenAI) に **Instagramストーリー規格（1080×1920, 9:16）の縦サムネ画像** を作らせるツール。
右下にはSpotifyロゴと "Let's listen on Spotify!" のCTA、上部にプレイリスト名と作者名がレンダリングされる。

Spotifyへの**ユーザーアカウント連携は不要**。Spotify Developer Dashboardでアプリ登録して得られる
Client ID / Secret （Client Credentials Flow）だけで動く。

---

## できること

- 公開プレイリストURLを1本入れるだけで縦サムネが完成
- 背景アートは Gemini ( `gemini-2.5-flash-image` = nano banana, 無料tier可) または `gemini-3-pro-image-preview` (nano banana pro, 有料)
- もしくは OpenAI `gpt-image-1` ( ChatGPT の画像生成と同じモデル) でも生成可能
- 文字とSpotifyロゴはPILで合成するのでブランド表記が崩れない

## 動かし方（Colab）

[`EnMP_banana.ipynb`](./EnMP_banana.ipynb) を Colab で開いて、上から実行するだけ。
必要なのは以下3つの鍵：

| 鍵 | 取り方 |
|---|---|
| Spotify Client ID / Secret | https://developer.spotify.com/dashboard で Create app → コピー（Redirect URIは任意の値でOK） |
| Gemini API key | https://aistudio.google.com/apikey （無料tierでOK） |
| OpenAI API key | https://platform.openai.com/api-keys （`gpt-image-1` 使用時のみ） |

`PROVIDER = "gemini"` か `"openai"` を切り替えるだけで生成エンジンが切り替わる。

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
    provider="gemini",                    # or "openai"
    gemini_api_key="...",
    # openai_api_key="...",
    output_path="playlist_thumbnail.png",
)
print(meta.name, "by", meta.owner, "/ genres:", meta.top_genres)
```

## 仕組み

1. `spotipy` で Client Credentials 認証 → プレイリストの全トラックをページング取得
2. アーティスト endpoint からジャンルを集めて多数決でtop genres決定
3. アルバム画像を最大6枚ダウンロード
4. 背景アート生成
   - Gemini: アルバム画像を入力として渡しつつ「文字は描くな・上中央と右下は空ける」と指示
   - OpenAI: `images.edit` にアルバム画像を入力として渡して同様のプロンプト
5. PIL で 1080×1920 にリサイズ → 上下にうっすら黒グラデ → タイトル / 作者 / ジャンル / Spotifyロゴ + "Let's listen on Spotify!" を合成

## モデル選択ガイド

| 用途 | 推奨 |
|---|---|
| 無料でとりあえず動かしたい | `gemini-2.5-flash-image` (Gemini無料tier) |
| 画質と質感重視 | `gemini-3-pro-image-preview` (nano banana pro, 有料) |
| ChatGPTで揃えたい | `gpt-image-1` (OpenAI) |

## ライセンス

MIT
