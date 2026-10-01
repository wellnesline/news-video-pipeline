# news_pipeline.sources — scout agent 方式に移行
#
# yfinance / JQuants / TDnet への直接アクセスは廃止。
# データ取得は news-scout-jp / news-scout-us エージェントが Web research で行い、
# news_config.json + script.json を work_dir に書き出す。
# variants/cut_a.py および variants/cut_b.py はその JSON を読み込むだけ。
#
# このパッケージは後方互換のために残しているが、モジュールは存在しない。
