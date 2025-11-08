## Service Booking & Estimation App
Flask + SQLite + HTMX + Tailwind 構成で構築した予約・見積り・案件管理アプリです。

**Features**
- 公開フォーム（リアルタイム見積もり）
- 管理画面（Basic認証・検索・ステータス管理・CSV出力）
- SQLite 自動生成（Render対応）
- デザイン: Tailwind CSS / レスポンシブ対応

**Demo**
👉 https://service-portfolio-app.onrender.com

### 管理画面
- パス: `/admin`
- Basic 認証のユーザー名/パスワードは環境変数 `ADMIN_USER`, `ADMIN_PASS` で設定


**Tech Stack**
- Flask / SQLAlchemy / HTMX / Tailwind
- Gunicorn (Render deploy)
- Environment: Render (Free Plan)
