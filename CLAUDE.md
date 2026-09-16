# CLAUDE.md — 防災・交通情報モニタ

水先人会の事務所に常設するモニタ用ボード。スクロール・クリックなしの一画面表示。
利用者向けの説明は README.md、ここは開発を引き継ぐための技術メモ。

## 構成
- `bousai_board.py`：Python 3.9+、標準ライブラリのみ（Windows PCで常駐させる前提。外部ライブラリを増やさない）
  - `src_*()` 関数がデータ源ごとに1つ。スレッドで `intervals` ごとに実行し、結果を `STATE` に保持
  - `build_status()` が `/api/status` のJSONを組み立てる。発令条件の判定は `build_alerts()`
  - 取得失敗時は前回値を残す。画面へは4段階で伝える：`failing`（直近1回の失敗。データはまだ新しいので
    フラグ「取得再試行中」だけ）／`partial`（警報の一部が直近1回取れていない。「河川情報を確認中」）／
    `unknown`（その取得元が `interval*3`・最低180秒より古い、または一度も取れていない。
    「河川情報を取得できていません」＋破線ランプ）／`stale`（系統ごと古い。「判定できません」）。
    単発の失敗で画面がざらつかないよう `stale` の閾値は据え置き、フッターは `failing`・`partial` の間
    「正常」と言わない
  - 警報は愛知・三重・河川の3か所を `_warn_part()` で別々に保持する。1か所落ちても取れた分は反映する。
    鮮度の判定はタイル単位。`unknown`（レベル5＝3か所すべて）と `unknown_wx`（気象特別警報＝愛知・三重だけ。
    河川は無関係なので見ない）を返す。`run_source` の `updated` は全系統共通の意味のまま触らない
  - JMAの一覧系は ETag/Last-Modified で差分取得（`http_get(conditional=True)`）。詳細電文は `get_detail()` でキャッシュ
- `bousai_board.html`：15秒ごとに `/api/status` を取得して描画。取得が重なったときは通し番号で
  「最後に描画したものより古い応答」だけを捨てる（古い応答で警報表示を平常に戻さない）。
  「次の取得が始まったら捨てる」にすると、応答が毎回15秒を超えたとき何も描けなくなるので不可。
  サイズ変更は300msまとめ。`fit()` がはみ出しを検知して文字縮小→詳細を1行化→詳細を隠す（`clip`）→「ほか n 件」。
    対象は発令条件タイル・道路（`[data-trim]` を付けた一覧）。黙って末尾が切れると「出ていない」と読めるため最後は必ず件数を出す。
    鉄道は `fitRail()` で列ごとに長い詳細を3行→1行→非表示にし、路線名・状態を優先。詳細を除いても収まらない場合のみ文字を縮小し、路線は件数にまとめない。設定で路線を増やす場合は実機の収まりを確認する
  取れていない系統は `pendingText()` で灰色にする（未取得は「取得中」、失敗後と stale は「判定できません」）。鉄道は stale の系統の
  平常・時間外の行を「判定できません」に替え、見合わせ・遅れの行は安全側に残す。緑の点は「取れていて平常」のときだけ出す
- `?demo=1`：本物のデータに警報・運休のダミーを重ねて表示（レイアウト確認用）
- `config.json`：利用者が触る設定。既定値は `DEFAULT_CONFIG`
- `kaze_board.py` / `kaze_board.html`：風向・風速ボード（2枚目の画面。設計の経緯は `WIND_BOARD_HANDOVER.md`）
  - `bousai_board` の `http_get`・`Handler`・`load_config` を流用。ポートは `wind_port`、ログは `kaze_board.log`（防災と共有しない）
  - 鮮度は観測間隔ベース（名古屋 20分で再試行中／35分で判定不可、四日市 35分／65分）。「風弱く」は正常な観測で欠測と区別する
  - `kaze_history.json` に24時間分を保存。中身が変わった取得と、前回の保存に失敗したときだけ書き直す
  - 画面は最後の応答から60秒で「通信断」を出し、6時間ごとに再読み込みする（防災画面と同じ）
  - 状態の出し方：注意ライン以上はパネルの縁・札・最大値タイル・グラフのライン上部を琥珀色にする（古い値では出さない）。
    判定できないときは風速の右に破線の札を出し、風配図とグラフを沈める。取得再試行中は観測時刻の横に言葉だけ出す。
    「風弱く」は落ち着いた色で、風向は「なし」。過去の風向線は1方向1本で、矢印が無いときは同じ薄さにする（針に見せない）
  - 数字は `.d`（1文字分の枠）に入れる。BIZ UDPゴシックは「1」だけ狭く、tnum も持たないため。ノットは整数に「約」を付ける
  - 焼き付き対策に、画面全体を10分ごとに最大±2pxずらす（`ORBIT`。表示確認モードでは止める）。`?demo=normal|1|high|retry`
- `launch_boards.py`：`start_board.bat` の本体。応答しないサーバーだけ、`--server` 付きの自分を監視役として起動する（落ちたら10秒後に再起動）。
  `/api/config` が応答してから Edge を開く（風向は `"board":"wind"` まで確認）。応答中のサーバーはそのまま使うので、
  手動で起動した古いサーバーが残っていると新しいコードに替わらない。`config.json` が壊れていたら防災だけ既定値で開き、終了コード1で止める
  （風向は有効か読めず、位置も読めないので、開くと防災画面に重なるおそれがある）
- `stop_boards.py`：`stop_boards.bat` の本体。スクリプトの絶対パスと Edge のプロファイルで所属を確かめて止める。
  相対パスで単独起動したサーバーは対象外（2026-09-16 に、別環境から相対パスで起動された古い風向サーバーが残り、`board.log` に書き込んでいた）
- 起動時の失敗は必ずログファイルに書く。起動バッチ経由では標準エラーが捨てられるので、書かないと再起動が黙って続く。
  `config.json` の読み込みは import 時（ログの出力先が決まる前）なので、理由を `CONFIG_ERROR` に残して各 `main()` で書き直す。
  防災サーバーはポートを確保してから取得スレッドを起動する（二重起動ですぐ終わるプロセスに各社へアクセスさせない）

## 発令条件（依頼者の指定。勝手に変えない）
1. 愛知県・三重県北中部が対象
   - 最大震度5弱以上、または長周期地震動階級3以上
   - 予想される津波の最大波が3m超（＝大津波警報）
   - レベル5特別警報（河川氾濫・大雨・土砂災害・高潮）
   - 特別警報（気象）：暴風・波浪・暴風雪・大雪
2. 全国で震度6弱以上

## データ源と注意点
| 対象 | URL | メモ |
|---|---|---|
| 警報 | `jma.go.jp/bosai/warning/data/r8/{230000,240000}.json` | **2026-05-29 の新防災気象情報で形式変更**。旧 `warning/data/warning/*.json` は5/28で更新停止。配列（VPWW55〜61の複数電文）。class10Items/class20Items の kinds[].code と status（"解除"は除外） |
| 警報コード | — | Lv5：33大雨 39土砂 38高潮／Lv4：43 49 48／気象特別警報：35暴風 32暴風雪 36大雪 37波浪。気象庁サイトJS内の対応表から取得 |
| 河川氾濫 | `bosai/flood/data/r8/flood_xml.json` | item.code 51/53=レベル5氾濫特別警報、40/41=レベル4。class10Codes で地域判定。**河川名のキーは未確認**（平常時は空配列で実物を見られていない） |
| 地域 | `bosai/common/const/area.json` | class20→class15→class10。対象 class10：230010 230020 240010 |
| 地震 | `bosai/quake/data/list.json` ＋詳細JSON | maxi は "5-" 形式。1地震（eid）に 震度速報→震源に関する情報→震源・震度情報 と複数電文が入るので、`_final_reports()` で震度を持つ最新の電文を確定値にする（下方修正も反映。取消電文があれば地震ごと除外）。三重は詳細電文の Area.Code で判定（450愛知東部 451愛知西部 460三重北部 461三重中部。2026-09-13 に461を含め全コード確認済み）。詳細電文が取れないときは県単位（list.json の int[].code 23/24）で判定するため、三重県南部だけの震度5弱以上も「三重県（区域は確認中）」として発令表示になる（安全側の意図的な動作） |
| 長周期 | `bosai/ltpgm/data/list.json` | lg[].code / maxLg |
| 津波 | `bosai/tsunami/data/list.json` ＋詳細 | 最新の VTSE41。Body.Tsunami.Forecast.Item[].Area.Name / Category.Kind.Name / MaxHeight.TsunamiHeight。対象：伊勢・三河湾、愛知県外海 |
| JR東海 | `traininfo.jr-central.co.jp/zairaisen/data/trainInfo/json/unkou.json` | 平常時 events=null。events[].imp_line/status/cause、message_info[i].delivery_msg。路線マスタ `hp_senku_master_ja.json` |
| 地下鉄 | `kotsu.city.nagoya.jp/datas/latest_traffic.json` | rosen_id（M_LINE＝名城・名港）、icon_cd N/W/C/E |
| 名鉄 | `top.meitetsu.co.jp/em/`（HTML） | JSONなし。平常は `p.emLv00`「15分以上の列車の遅れはございません。」。異常時は `div.emInfo.emLvNN` の中に h2＝状態、`ul.emListLine>li`＝対象線区、表の「路線／理由／備考」。レベルは CSS の定義で 01 運転見合せ／02 遅延・一部運休／03 振替輸送／04 バス代行輸送／05 その他（2026-09-13 `em/css/basic.css` で確認）。**停止判定はレベル番号 01 で行う**（名鉄は「見合せ」と送り仮名なしで書くため文言判定は保険扱い）。線区判定は emListLine を使う（文章マッチは誤検知するので戻さない）。どちらも無ければ構造変更とみなして例外。提供時間外（0:31〜4:59）の文言差し替えは**ページ内JSが `#descriptionText` を書き換えているだけ**で取得HTMLには出ないため、JSと同じ条件を `off_hours` で判定する（2026-09-13 実物で確認） |
| あおなみ線 | `aonamiline.co.jp/railinfo`（HTML） | `table.delay_table` の td |
| 伊勢湾岸道 | `ihighway.jp/datas/json/traffic.json` | NEXCO中日本。trafficInfo/otherTrafficInfo のカテゴリ別。区間判定は `icInfoApp.json` の pointX（東海JCT 5489〜みえ川越 5278）と、イベントの coordinate または題名のIC名。フィールドは jam が `distance`(km)・`passing.section`/`passing.time`・`title`(渋滞先頭)、規制系が `detail`＋`reason`、その他は `title`＋`reason` |
| 国道23号 愛知側（保留） | `cbr.mlit.go.jp/meikoku/cms/{kisei,kinkyu}/?view=index` | 名古屋国道事務所。事故・渋滞は含まない。JARTIC は規約面で採用せず |
| 国道23号 三重側（未実装） | `cbr.mlit.go.jp/mie/kouji/` ／ `mie/emergency_lists.php` ／ カメラ `mie/live/touge/cam_data/Jpeg058.jpg` | **三重河川国道事務所**（北勢国道事務所ではない。北勢は1号・25号・475号のみ）。工事規制予定は毎日20時に翌日分、HTML表の「愛知県境〜四日市市中里」が みえ川越IC〜四日市千歳町（380〜393KP）。カメラは 392.5KP 四日市市海山道「四日市高架橋」（区間内で唯一、約15分更新）。事故・渋滞は無し |

## 開発時の確認方法
- 各 `src_*()` を単体で呼んで実データを確認できる（`python -c "import bousai_board as b; print(b.src_jr())"`）
- テストは `python -m unittest -v test_bousai_board test_kaze_board test_launch_boards test_stop_boards test_board_layout`
  （`test_board_layout` は Playwright と Edge が必要。`test_stop_boards` は Windows 専用）
- 発令条件の判定は `test_bousai_board` で `get_json`／`get_text` を差し替えて確認する（地震の最終報・取消・区域、津波、Lv5/Lv4、河川、
  気象特別警報、取得元ごとの前回値と unknown、名鉄・あおなみ線・JR・地下鉄の読み取り）。判定を変えたらここも更新する
- 1920×1080 で `?demo=1` を表示し、はみ出しがないこと

## 未対応・要確認
- [x] 名鉄の異常時HTML構造を確認し、線区判定を `ul.emListLine` ベースに変更（2026-09-11 の遅延時に実物を確認）
- [ ] 河川氾濫の河川名キー（`flood_xml.json` は平常時 `[]`。発生時にしか確認できない）
- [x] 三重県中部 461：実データで確認（2026-09-13）。`QUAKE_AREAS` の4コードはすべて裏付けが取れた
- [ ] 国道23号（保留中。何を出すか未決）。情報源は 2026-09-11 に調査済みで上表のとおり。事故・渋滞のリアルタイムは Google地図の交通状況（`google_maps_api_key`、実装済み）以外に手段なし。国交省 道路情報提供システム `road-info-prvs.mlit.go.jp`（5〜10分更新）はデータが毎回変わる難読パスの下で壊れやすく不採用。三重県道路規制情報は県管理道路のみで23号は対象外
- [x] ログのファイル出力（`board.log`。1MB×3世代でローテーション。2026-09-12）
- [ ] PC起動時の自動実行の手順確認（`start_board.bat`→`launch_boards.py` はサーバーが応答するまで待ってから開く。打ち切りはしない
  ――未起動のまま開くとキオスクがエラー画面で固まり、ページが読まれないので自動更新も動かないため。
  接続先ポートは `config.json` の `port`／`wind_port` から読む。タスクスケジューラ／スタートアップへの登録手順と、3画面への配置は未確認）
- [ ] 二重起動の防止（起動ロックなし。サーバーが応答する前に `start_board.bat` を続けて実行すると監視役が重複し、
  後から起動したサーバーはポート競合で10秒ごとに再起動を繰り返す。理由はログに残る）
