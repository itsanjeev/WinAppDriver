"""
FlaUIOptimized — Performance wrapper on top of robotframework-flaui.
====================================================================
Inherits from FlaUILibrary, adds:
  - Scoped search (narrow to panel before finding)
  - Element caching (find once, reuse within test)
  - Bulk find (one walk, many elements)
  - Smart waits (replace Sleep with condition polling)
  - AutomationId discovery helpers

Install:
    pip install robotframework-flaui

Usage in .robot:
    *** Settings ***
    Library    FlaUIOptimized    WITH NAME    FlaUI
    # All existing RF-FlaUI keywords still work as-is
    # Plus you get the new optimized keywords below
"""

import time
from robot.api import logger
from robot.api.deco import keyword
from FlaUILibrary import FlaUILibrary


class FlaUIOptimized(FlaUILibrary):
    """
    Extends robotframework-flaui with performance optimizations.
    All original RF-FlaUI keywords remain available unchanged.
    """

    ROBOT_LIBRARY_SCOPE = 'GLOBAL'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._element_cache = {}
        self._panel_cache = {}
        self._scope_stack = []

    # =================================================================
    # INTERNAL — Access FlaUI internals from parent library
    # =================================================================

    def _get_automation(self):
        """Get the UIA3Automation instance from RF-FlaUI internals."""
        return self.module.automation

    def _get_main_window(self):
        """Get the current main window from RF-FlaUI."""
        return self.module.window

    def _get_condition_factory(self):
        """Get ConditionFactory for building search conditions."""
        return self._get_automation().ConditionFactory

    # =================================================================
    # SCOPED SEARCH — The biggest performance win
    # =================================================================

    @keyword("Set Search Scope")
    def set_search_scope(self, panel_identifier):
        """
        Narrow all subsequent Find operations to a specific panel.
        This avoids walking 2000 elements when you only need 80.

        Args:
            panel_identifier: AutomationId or Name:value of the panel

        Example:
            Set Search Scope    propertiesPanel
            # All finds now search within propertiesPanel only
            ${el}=    Find By AutomationId Fast    txtMeshSize
            Click    ${el}
            Clear Search Scope
        """
        panel = self._resolve_panel(panel_identifier)
        if panel:
            self._scope_stack.append(panel)
            logger.info(f"Search scope set to: {panel_identifier}")
        else:
            raise RuntimeError(f"Panel not found: {panel_identifier}")

    @keyword("Clear Search Scope")
    def clear_search_scope(self):
        """Reset search scope to full window."""
        if self._scope_stack:
            self._scope_stack.pop()
            logger.info("Search scope cleared.")

    @keyword("Find In Scope")
    def find_in_scope(self, identifier, timeout=10):
        """
        Find element within current scope using RF-FlaUI identifier format.
        Supports the same identifier syntax as standard RF-FlaUI:
            AutomationId:value, Name:value, ClassName:value

        Example:
            Set Search Scope    propertiesPanel
            ${el}=    Find In Scope    Name:Mesh Size
            ${el}=    Find In Scope    AutomationId:txtMeshSize
        """
        parent = self._current_scope()
        cf = self._get_condition_factory()
        condition = self._parse_identifier_to_condition(identifier, cf)

        return self._find_with_retry(
            lambda: parent.FindFirstDescendant(condition),
            identifier,
            float(timeout)
        )

    # =================================================================
    # FAST FIND KEYWORDS — AutomationId direct lookup
    # =================================================================

    @keyword("Find By AutomationId Fast")
    def find_by_automation_id_fast(self, auto_id, scope=None, timeout=10):
        """
        Direct AutomationId lookup — 10-20x faster than Name search.

        Args:
            auto_id: AutomationId of the element
            scope:   Optional panel AutomationId to search within
            timeout: Max wait seconds

        Example:
            ${btn}=    Find By AutomationId Fast    btnRunAnalysis
            ${el}=     Find By AutomationId Fast    txtMeshSize    scope=propertiesPanel
        """
        parent = self._resolve_scope(scope)
        cf = self._get_condition_factory()

        return self._find_with_retry(
            lambda: parent.FindFirstDescendant(cf.ByAutomationId(auto_id)),
            f"AutomationId='{auto_id}'",
            float(timeout)
        )

    @keyword("Find By Name Scoped")
    def find_by_name_scoped(self, name, control_type=None,
                             scope=None, timeout=10):
        """
        Name search but scoped to a panel — much faster than root search.

        Example:
            ${el}=    Find By Name Scoped    Mesh Size    scope=propertiesPanel
            ${el}=    Find By Name Scoped    OK    control_type=Button    scope=dialogPanel
        """
        parent = self._resolve_scope(scope)
        cf = self._get_condition_factory()

        if control_type:
            from FlaUI.Core.Definitions import ControlType
            ct = getattr(ControlType, control_type)
            condition = cf.ByNameAndControlType(name, ct)
        else:
            condition = cf.ByName(name)

        return self._find_with_retry(
            lambda: parent.FindFirstDescendant(condition),
            f"Name='{name}'",
            float(timeout)
        )

    # =================================================================
    # BULK FIND — One tree walk for multiple elements
    # =================================================================

    @keyword("Bulk Find By AutomationIds")
    def bulk_find_by_automation_ids(self, *auto_ids, scope=None):
        """
        Find multiple elements in ONE tree walk.
        Returns a dictionary: { auto_id: element }.

        Before: 10 elements × 2s each = 20s
        After:  1 walk = ~2-3s total for all 10

        Example:
            ${els}=    Bulk Find By AutomationIds
            ...    btnRun    btnStop    txtStatus    cmbType
            Click    ${els}[btnRun]
            Set Text Fast    ${els}[txtStatus]    Ready
        """
        parent = self._resolve_scope(scope)
        all_elements = parent.FindAllDescendants()
        target_set = set(auto_ids)
        results = {}

        for el in all_elements:
            aid = el.Properties.AutomationId.ValueOrDefault
            if aid and aid in target_set:
                results[aid] = el
                target_set.discard(aid)
                if not target_set:
                    break

        if target_set:
            logger.warn(f"Not found in bulk search: {target_set}")

        logger.info(f"Bulk find: {len(results)}/{len(auto_ids)} elements found")
        return results

    # =================================================================
    # ELEMENT CACHING — Find once, reuse across steps
    # =================================================================

    @keyword("Get Or Cache Element")
    def get_or_cache_element(self, cache_key, auto_id=None, scope=None):
        """
        Return cached element or find and cache it on first call.
        Validates the cached reference is still alive.

        Example:
            # First call: finds and caches
            ${btn}=    Get Or Cache Element    run_btn    auto_id=btnRunAnalysis

            # Later calls: instant return from cache
            ${btn}=    Get Or Cache Element    run_btn
            Click    ${btn}
        """
        if cache_key in self._element_cache:
            el = self._element_cache[cache_key]
            if self._is_element_alive(el):
                return el
            else:
                del self._element_cache[cache_key]
                logger.debug(f"Cached element '{cache_key}' is stale, re-finding.")

        if not auto_id:
            raise ValueError(
                f"'{cache_key}' not in cache. Provide auto_id for first lookup."
            )

        el = self.find_by_automation_id_fast(auto_id, scope=scope)
        self._element_cache[cache_key] = el
        return el

    @keyword("Clear Element Cache")
    def clear_element_cache(self):
        """Clear cached elements and panels. Use in [Test Setup]."""
        count = len(self._element_cache)
        self._element_cache.clear()
        self._panel_cache.clear()
        self._scope_stack.clear()
        logger.info(f"Cleared {count} cached elements.")

    # =================================================================
    # SMART WAITS — Replace Sleep with condition polling
    # =================================================================

    @keyword("Wait Until Element Text Contains")
    def wait_until_element_text_contains(self, element, expected,
                                          timeout=60, poll_interval=1):
        """
        Poll element text until it contains expected value.
        Replaces: Sleep 120s

        Example:
            ${status}=    Find By AutomationId Fast    lblStatus
            Wait Until Element Text Contains    ${status}    Complete    timeout=120
        """
        deadline = time.time() + float(timeout)
        poll = float(poll_interval)

        while time.time() < deadline:
            try:
                current = self._get_element_text(element)
                if expected in current:
                    logger.info(f"Text matched: found '{expected}' in '{current}'")
                    return current
            except Exception as e:
                logger.debug(f"Poll error: {e}")
            time.sleep(poll)

        raise TimeoutError(
            f"Element text did not contain '{expected}' within {timeout}s. "
            f"Last value: '{current}'"
        )

    @keyword("Wait Until Element Enabled")
    def wait_until_element_enabled(self, element, timeout=30, poll_interval=0.5):
        """
        Wait for an element to become enabled.

        Example:
            ${btn}=    Find By AutomationId Fast    btnExport
            Wait Until Element Enabled    ${btn}    timeout=30
            Click    ${btn}
        """
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            try:
                if element.Properties.IsEnabled.Value:
                    return True
            except Exception:
                pass
            time.sleep(float(poll_interval))
        raise TimeoutError(f"Element not enabled within {timeout}s")

    @keyword("Wait Until Element Exists")
    def wait_until_element_exists(self, auto_id, scope=None, timeout=30):
        """
        Wait for an element to appear in the UI tree.

        Example:
            Wait Until Element Exists    dlgExportComplete    timeout=60
        """
        return self.find_by_automation_id_fast(auto_id, scope=scope, timeout=timeout)

    # =================================================================
    # FAST INTERACTION KEYWORDS
    # =================================================================

    @keyword("Set Text Fast")
    def set_text_fast(self, element, text):
        """
        Set text using Value pattern (faster than keystroke sim).

        Example:
            ${field}=    Find By AutomationId Fast    txtMeshSize
            Set Text Fast    ${field}    2.5
        """
        try:
            value_pattern = element.Patterns.Value
            if value_pattern.IsSupported:
                value_pattern.Pattern.SetValue(str(text))
                return
        except Exception:
            pass
        element.AsTextBox().Text = str(text)

    @keyword("Get Text Fast")
    def get_text_fast(self, element):
        """
        Get text using Value pattern (avoids screen scraping).

        Example:
            ${val}=    Get Text Fast    ${field}
        """
        return self._get_element_text(element)

    @keyword("Click Fast")
    def click_fast(self, element, use_invoke=${True}):
        """
        Click via Invoke pattern (no mouse movement, faster).
        Falls back to .Click() if Invoke not supported.

        Example:
            Click Fast    ${btn}
        """
        if use_invoke:
            try:
                invoke = element.Patterns.Invoke
                if invoke.IsSupported:
                    invoke.Pattern.Invoke()
                    return
            except Exception:
                pass
        element.Click()

    # =================================================================
    # DISCOVERY — Run once to map your locators
    # =================================================================

    @keyword("Dump All AutomationIds")
    def dump_all_automation_ids(self, scope=None, filepath=None):
        """
        List all elements with AutomationIds. Run ONCE to build locator map.

        Example:
            Dump All AutomationIds    filepath=moldflow_elements.txt
            Dump All AutomationIds    scope=propertiesPanel
        """
        parent = self._resolve_scope(scope)
        all_elements = parent.FindAllDescendants()
        lines = []

        for el in all_elements:
            aid = el.Properties.AutomationId.ValueOrDefault
            if not aid:
                continue
            name = el.Properties.Name.ValueOrDefault or ""
            ctrl = el.Properties.ControlType.ValueOrDefault or ""
            visible = not el.Properties.IsOffscreen.ValueOrDefault

            line = (f"AutoId: {str(aid):<35} "
                    f"Name: {str(name):<30} "
                    f"Type: {str(ctrl):<15} "
                    f"Visible: {visible}")
            lines.append(line)
            logger.info(line)

        if filepath:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            logger.info(f"Wrote {len(lines)} elements to {filepath}")

        return lines

    @keyword("Dump Panel Structure")
    def dump_panel_structure(self, depth=2, filepath=None):
        """
        Show top-level panel hierarchy — helps identify scope targets.

        Example:
            Dump Panel Structure    depth=2
        """
        window = self._get_main_window()
        lines = []
        self._walk_tree(window, 0, int(depth), lines)

        for line in lines:
            logger.info(line)

        if filepath:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))

        return lines

    # =================================================================
    # INTERNAL HELPERS
    # =================================================================

    def _current_scope(self):
        if self._scope_stack:
            return self._scope_stack[-1]
        return self._get_main_window()

    def _resolve_scope(self, scope_auto_id):
        if scope_auto_id:
            return self._resolve_panel(scope_auto_id)
        return self._current_scope()

    def _resolve_panel(self, panel_auto_id):
        if panel_auto_id in self._panel_cache:
            panel = self._panel_cache[panel_auto_id]
            if self._is_element_alive(panel):
                return panel
            del self._panel_cache[panel_auto_id]

        window = self._get_main_window()
        cf = self._get_condition_factory()
        panel = window.FindFirstDescendant(cf.ByAutomationId(panel_auto_id))

        if panel:
            self._panel_cache[panel_auto_id] = panel
            return panel

        # Fallback: try by Name
        panel = window.FindFirstDescendant(cf.ByName(panel_auto_id))
        if panel:
            self._panel_cache[panel_auto_id] = panel
            return panel

        return None

    def _parse_identifier_to_condition(self, identifier, cf):
        if ':' in identifier:
            strategy, value = identifier.split(':', 1)
            strategy = strategy.strip().lower()
            value = value.strip()

            if strategy == 'automationid':
                return cf.ByAutomationId(value)
            elif strategy == 'name':
                return cf.ByName(value)
            elif strategy == 'classname':
                return cf.ByClassName(value)

        return cf.ByAutomationId(identifier)

    def _find_with_retry(self, find_func, desc, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            el = find_func()
            if el is not None:
                return el
            time.sleep(0.3)
        raise TimeoutError(f"Element not found: {desc} within {timeout}s")

    def _is_element_alive(self, element):
        try:
            _ = element.Properties.IsOffscreen.Value
            return True
        except Exception:
            return False

    def _get_element_text(self, element):
        try:
            value_pattern = element.Patterns.Value
            if value_pattern.IsSupported:
                return value_pattern.Pattern.Value or ""
        except Exception:
            pass
        return element.Properties.Name.ValueOrDefault or ""

    def _walk_tree(self, element, level, max_depth, lines):
        if level > max_depth:
            return
        indent = "  " * level
        aid = element.Properties.AutomationId.ValueOrDefault or ""
        name = element.Properties.Name.ValueOrDefault or ""
        ctrl = element.Properties.ControlType.ValueOrDefault or ""
        lines.append(f"{indent}[{ctrl}] Name='{name}' AutoId='{aid}'")
        try:
            for child in element.FindAllChildren():
                self._walk_tree(child, level + 1, max_depth, lines)
        except Exception:
            pass
