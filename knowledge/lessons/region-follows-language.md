# 地区靠关键词语言定位，不靠 region 参数

英文 study with me 与韩语 공부법 的结果语言倾向不同。viral 的 region 不能当作可靠结果过滤器。
scout/viral.py 的 search_videos() 不暴露 region；市场过滤仍在本地按视频返回的 region 字段执行。
使用目标市场语言，从赛道、场景、痛点和话题标签等角度给出 3–5 个词，避免宽泛词产生大量无关结果。
