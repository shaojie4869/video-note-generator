from study_buddy.syllabus408 import match_syllabus, syllabus_block


def test_matches_os_structure_chapter():
    chapter = match_syllabus(
        "操作系统精讲课",
        "1-4 操作系统结构",
        "微内核只保留进程切换、中断处理和 IPC；分层式结构每层只能调用下一层。",
    )
    assert chapter is not None
    assert chapter.subject == "操作系统"
    assert "第1章" in chapter.chapter
    assert any("微内核" in item for item in chapter.must)


def test_matches_search_chapter_for_data_structure_course():
    chapter = match_syllabus(
        "数据结构强化课",
        "4-查找",
        "折半查找的 ASL、分块查找、B 树与 B+ 树的区别、散列表装填因子。",
    )
    assert chapter is not None
    assert chapter.subject == "数据结构"
    assert "查找" in chapter.chapter


def test_matches_sorting_chapter_by_title():
    chapter = match_syllabus("", "8-3 选择排序", "堆排序的建堆与调整，稳定性对比。")
    assert chapter is not None
    assert "排序" in chapter.chapter


def test_unrelated_content_gets_no_syllabus():
    assert match_syllabus("A股复盘", "2024 年市场回顾", "北向资金、市盈率与行业轮动。") is None
    block = syllabus_block("A股复盘", "2024 年市场回顾", "北向资金与市盈率。")
    assert "未匹配到具体章节" in block


def test_syllabus_block_states_scope_rule():
    block = syllabus_block(
        "操作系统精讲课",
        "1-4 操作系统结构",
        "微内核、分层式结构、模块化、外核的优缺点。",
    )
    assert "408 考纲定位" in block
    assert "必须掌握" in block
    assert "考纲里有但本节没讲的内容不要出题" in block
