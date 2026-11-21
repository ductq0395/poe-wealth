# PoE Wealth (local)

Bản tự chạy kiểu Wealthy Exile cho PoE 1. Mọi thứ chạy trên máy, không cần login PoE vào trang khác.

- **POESESSID** lưu ở `~/.config/poe-wealth/config.json` (chmod 600), chỉ gửi tới `www.pathofexile.com`. Web UI không bao giờ nhận lại cookie.
- **Giá** lấy từ poe.ninja (API công khai, không gửi cookie).
- **Timeless jewel** check giá trên trade site theo seed + conqueror (Wealthy Exile không làm phần này).
- Server chỉ nghe `127.0.0.1:8787`, có chặn DNS-rebinding/CSRF.
- Dữ liệu: `~/.config/poe-wealth/wealth.sqlite3`.

## Chạy (fish)

```fish
sudo pacman -S --needed python-pipx
pipx ensurepath            # thêm ~/.local/bin vào PATH, mở lại terminal
pipx install -e ~/Downloads/poe-wealth
poe-wealth
# mở http://127.0.0.1:8787 trong Edge
```

`-e` (editable) giúp sửa code trong `~/Downloads/poe-wealth` có hiệu lực ngay. Chỉ cần tắt rồi chạy lại `poe-wealth`, không phải cài lại.
Nếu sau này đổi dependency trong `pyproject.toml`: `pipx install -e --force ~/Downloads/poe-wealth`.
Gỡ cài đặt: `pipx uninstall poe-wealth`. Dữ liệu trong `~/.config/poe-wealth` vẫn giữ nguyên.

Đổi port (fish): `env POE_WEALTH_PORT=8790 poe-wealth`.

## Lần đầu

1. Edge → pathofexile.com (đăng nhập) → F12 → Application → Cookies → copy `POESESSID`.
2. Tab **Cài đặt**: dán POESESSID, account name **có #số** (xem ở góc trang profile), bấm “Lấy list” để chọn league → Lưu.
3. “Tải danh sách tab” → tick các tab muốn tính. “Tải nhân vật” nếu chỉ muốn tính vài nhân vật.
4. Bấm **Snapshot**. Lần đầu tải giá poe.ninja mất khoảng 10–20 giây; mỗi tab là 1 request, có rate limit theo header của GGG.

## Tính năng

- **Tổng quan**: net worth theo thời gian (div/chaos), chaos mỗi giờ, giá trị theo tab/nhân vật, top item.
- **Item**: lọc theo nguồn, loại, tên, giá tối thiểu.
- **Phiên farm**: bấm Bắt đầu/Dừng; lời và lời/giờ = snapshot cuối − đầu. Bấm vào một phiên để xem item tăng/giảm.
- **Timeless**: từng jewel kèm seed/conqueror, trạng thái và giá. Search theo đúng conqueror; nếu không có listing thì search theo seed với mọi conqueror cùng loại. Giá = trung vị 5 listing online rẻ nhất (bỏ listing rẻ nhất). Cache 6 giờ, chạy dần ở nền vì trade search bị giới hạn khoảng 1 lần/6 giây.
- **Auto snapshot**: đặt số phút trong Cài đặt (app phải đang chạy).

## Giới hạn đã biết

- Dùng API cũ `character-window` (giống các tool như Exilence/Wealthy Exile bản cookie). Map tab, Unique tab và folder có thể không trả item.
- Giá rare chỉ tính theo base type khi ilvl ≥ 82 (giá base poe.ninja). Rare có mod tốt sẽ bị định giá thấp hơn thực tế.
- Cluster jewel, beast và item không có trên poe.ninja = 0.
- poe.ninja dùng API mới `poe.ninja/poe1/api/economy/{exchange|stash}` (API cũ `/api/data/...` đã trả 404). Nếu lại đổi, dòng trạng thái báo “poe.ninja lỗi …”: sửa `NINJA` và danh sách type trong `poewealth/pricing.py`.

## Chạy nền khi khởi động (tuỳ chọn)

`~/.config/systemd/user/poe-wealth.service`:

```ini
[Unit]
Description=PoE Wealth local

[Service]
ExecStart=%h/.local/bin/poe-wealth
Restart=on-failure

[Install]
WantedBy=default.target
```

```fish
systemctl --user daemon-reload
systemctl --user enable --now poe-wealth
```

## Patch game (minimap + màu dòng Eldritch altar)

Tab **Patch game**, port từ PoeRedux (https://github.com/Gineticus/PoeRedux):

- **Minimap**: hiện toàn bộ minimap ngay từ đầu (giống `poe_minimap.py`).
- **Màu dòng Eldritch altar**: tô màu 14 dòng của trụ Eater of Worlds / Searing Exarch theo nhóm của PoeRedux. Dòng divine màu xanh lá, dòng duplicate màu xanh dương, dòng nguy hiểm màu đỏ. Đổi được nhóm và màu.

Cách dùng:

1. Tắt game.
2. Nhập đường dẫn `Content.ggpk` (bản standalone) hoặc `Bundles2/_.index.bin` (Steam/Epic).
3. Bấm **Kiểm tra** rồi **Áp dụng**.

Sau mỗi lần game update phải bấm Áp dụng lại. Bản gốc được backup ở `~/.config/poe-wealth/game-backup/`; nút **Khôi phục bản gốc** trả lại tất cả. Nếu trước đó đã patch minimap bằng `poe_minimap.py`, app dùng luôn backup của script đó.

Cần thư viện `pyooz`: `pipx install -e --force ~/Downloads/poe-wealth` (hoặc `pipx inject poe-wealth pyooz`).

Sửa file game vi phạm ToS của GGG, có rủi ro bị ban.
