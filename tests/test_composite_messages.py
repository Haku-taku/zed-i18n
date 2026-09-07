import importlib
import textwrap
import unittest


PROJECT_RULES_FILE = "crates/agent_ui/src/conversation_view/thread_view.rs"
KEYBINDINGS_FILE = "crates/keymap_editor/src/keymap_editor.rs"
KEYBINDINGS_SOURCE = "There are {} keybindings with the same keystrokes."


def _keybindings_source() -> str:
    return textwrap.dedent(
        '''
        impl Render for KeybindingEditorModal {
            fn render(&mut self) {
                let label = format!(
                    "There {} {} {} with the same keystrokes.",
                    if matching_bindings_count == 1 { "is" } else { "are" },
                    matching_bindings_count,
                    if matching_bindings_count == 1 { "binding" } else { "bindings" }
                );
                Label::new(label);
            }
        }
        '''
    )


def _project_rules_source() -> str:
    return textwrap.dedent(
        """
        impl Render for TokenUsageTooltip {
            fn render(&mut self) {
                let unrelated = format!("{} {}", left, right);
                Button::new(
                    "open-project-rules",
                    format!(
                        "{} {}",
                        project_rules_count,
                        pluralize("project rule", project_rules_count)
                    ),
                );
            }
        }
        """
    )


class CompositeMessageTests(unittest.TestCase):
    def test_keybindings_extracts_count_and_maps_to_second_argument(self) -> None:
        from tools.zed_i18n.extract import extract_ui_strings_from_source

        module = self._module()
        source = _keybindings_source()
        matches = module.find_composite_message_matches(source.encode(), KEYBINDINGS_FILE)
        self.assertEqual([m.rule.id for m in matches], ["keymap.matching_bindings_count"])
        occurrences = extract_ui_strings_from_source(source, KEYBINDINGS_FILE)
        self.assertNotIn("There {} {} {} with the same keystrokes.",
                         [o.source for o in occurrences])
        self.assertEqual(sum(o.source == KEYBINDINGS_SOURCE for o in occurrences), 1)
        self.assertEqual(
            module.render_composite_translation(matches[0].rule, "같은 키 입력을 사용하는 키 바인딩이 {}개 있습니다."),
            "같은 키 입력을 사용하는 키 바인딩이 {1}개 있습니다.{0:.0}{2:.0}",
        )
        with self.assertRaisesRegex(ValueError, "placeholder mismatch"):
            module.render_composite_translation(matches[0].rule, "키 바인딩")

    def test_keybindings_rejects_changed_argument_producers_and_location(self) -> None:
        module = self._module()
        source = _keybindings_source()
        variants = (
            source.replace('"binding"', '"command"'),
            source.replace('"is"', '"was"'),
            source.replace("matching_bindings_count,", "other_count,"),
            source.replace("matching_bindings_count == 1", "other_count == 1"),
            source.replace("KeybindingEditorModal", "OtherModal"),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertEqual(
                    module.find_composite_message_matches(variant.encode(), KEYBINDINGS_FILE), []
                )
        self.assertEqual(module.find_composite_message_matches(source.encode(), "other.rs"), [])

    def _module(self):
        try:
            return importlib.import_module("tools.zed_i18n.composite_messages")
        except ModuleNotFoundError:
            self.fail("composite message module is missing")

    def test_matches_and_renders_project_rules_count(self) -> None:
        module = self._module()

        matches = module.find_composite_message_matches(
            _project_rules_source().encode(),
            PROJECT_RULES_FILE,
        )

        self.assertEqual([match.rule.id for match in matches], ["agent.project_rules_count"])
        self.assertEqual(
            module.render_composite_translation(matches[0].rule, "프로젝트 규칙 {}개"),
            "프로젝트 규칙 {0}개{1:.0}",
        )

    def test_rejects_near_matches(self) -> None:
        module = self._module()
        source = _project_rules_source()
        variants = (
            source.replace("open-project-rules", "other-button"),
            source.replace('"project rule"', '"workspace rule"'),
            source.replace(
                'pluralize("project rule", project_rules_count)',
                'pluralize("project rule", other_count)',
            ),
        )

        for variant in variants:
            with self.subTest(variant=variant):
                self.assertEqual(
                    module.find_composite_message_matches(
                        variant.encode(),
                        PROJECT_RULES_FILE,
                    ),
                    [],
                )

    def test_rewrites_only_the_verified_format_literal(self) -> None:
        module = self._module()

        rewritten = module.rewrite_composite_message_source(
            _project_rules_source(),
            PROJECT_RULES_FILE,
            "agent.project_rules_count",
            "프로젝트 규칙 {}개",
        )

        self.assertIsNotNone(rewritten)
        self.assertIn(
            '"프로젝트 규칙 {0}개{1:.0}",',
            rewritten,
        )
        self.assertIn('let unrelated = format!("{} {}", left, right);', rewritten)

    def test_returns_none_when_structure_drifts(self) -> None:
        module = self._module()
        source = _project_rules_source().replace(
            '"open-project-rules"',
            '"other-button"',
        )

        self.assertIsNone(
            module.rewrite_composite_message_source(
                source,
                PROJECT_RULES_FILE,
                "agent.project_rules_count",
                "프로젝트 규칙 {}개",
            )
        )

    def test_maps_two_visible_arguments_without_registering_the_rule(self) -> None:
        module = self._module()
        rule = module.CompositeMessageRule(
            id="test.two_counts",
            file="test.rs",
            enclosing_fn="render",
            fingerprint='format!("{} {} & {} {}",errors,error_suffix,warnings,warning_suffix)',
            ordinal=0,
            anchor=None,
            virtual_source="{} errors & {} warnings",
            kind="label",
            call="format!",
            visible_args=(0, 2),
            suppressed_args=(
                module.SuppressedArgument(1, "error_suffix", "string"),
                module.SuppressedArgument(3, "warning_suffix", "string"),
            ),
            translation_note="Two visible counts.",
        )

        self.assertEqual(
            module.render_composite_translation(
                rule,
                "{1}개 경고 및 {0}개 오류",
            ),
            "{2}개 경고 및 {0}개 오류{1:.0}{3:.0}",
        )


if __name__ == "__main__":
    unittest.main()
