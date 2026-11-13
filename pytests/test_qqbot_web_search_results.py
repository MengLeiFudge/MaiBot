"""核对搜索执行证据与响应完成状态，不联网、不调用模型。"""

from copy import deepcopy
from typing import Any
from unittest import TestCase

from plugins.qqbot_web_search.plugin import search_result


class SearchResultTests(TestCase):
    """模型正文和网址不能代替实际搜索记录。"""

    def setUp(self) -> None:
        """构造带有真实搜索记录、来源引用和内部思考的官方响应。"""
        self.response: dict[str, Any] = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "内部推理不应公开"}]},
                {"type": "web_search_call", "status": "completed", "kind": "deep_search",
                 "action": {"type": "search", "queries": ["Python asyncio.gather"]}},
                {"type": "message", "content": [{
                    "type": "output_text", "text": "gather 默认不取消其他任务。",
                    "annotations": [{"type": "url_citation", "title": "Python 文档",
                                     "url": "https://docs.python.org/3/library/asyncio-task.html"}],
                }]},
            ],
        }

    def test_complete_search_returns_public_answer_and_sources(self) -> None:
        """已完成的深度搜索可以返回正文和引用，但不包含推理。"""
        result = search_result(self.response)
        self.assertTrue(result["success"])
        self.assertIn("gather", str(result["content"]))
        self.assertIn("docs.python.org", str(result["sources"]))
        self.assertNotIn("内部推理", str(result))

    def test_completed_model_answer_without_search_is_rejected(self) -> None:
        """模型宣称查过并给出网址，仍不能冒充没有执行的搜索。"""
        response = deepcopy(self.response)
        response["output"] = [self.response["output"][-1]]
        self.assertFalse(search_result(response)["success"])

    def test_failed_search_call_is_not_evidence(self) -> None:
        """失败的服务器工具调用不能使普通模型正文成为检索结果。"""
        response = deepcopy(self.response)
        response["output"][1]["status"] = "failed"
        self.assertFalse(search_result(response)["success"])

    def test_token_truncation_is_reported_without_partial_answer(self) -> None:
        """思考耗尽输出余量时明确失败，不能发送未完成结论。"""
        response = deepcopy(self.response)
        response["status"] = "incomplete"
        response["incomplete_details"] = {"reason": "max_output_tokens"}
        result = search_result(response)
        self.assertFalse(result["success"])
        self.assertIn("token", str(result["content"]))
        self.assertNotIn("gather", str(result))

    def test_search_without_public_answer_is_rejected(self) -> None:
        """只有搜索活动而无可用正文时不能返回成功。"""
        response = deepcopy(self.response)
        response["output"] = response["output"][:2]
        self.assertFalse(search_result(response)["success"])

    def test_malformed_and_failed_responses_are_rejected(self) -> None:
        """HTTP 成功不代表响应完成，拒绝缺失及失败的结构。"""
        for response in (None, [], {}, {"status": "failed"}, {"status": "completed", "output": None}):
            with self.subTest(response=response):
                self.assertFalse(search_result(response)["success"])
