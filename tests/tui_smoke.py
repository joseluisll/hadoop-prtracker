"""Headless smoke test of pr_manager against the live PRs; every write is stubbed out."""
import asyncio, sys, os, tempfile
os.environ['JIRA_TOKEN'] = 'dummy-not-real'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fix_dependencies as fd
calls = []
def stub(*a, **k):
    calls.append(a); return True, "stubbed - nothing written"
fd.create_link = fd.delete_link = fd.update_pr_body = stub
import pr_manager as pm

async def wait_idle(app, pilot, secs=240):
    for _ in range(secs * 2):
        await pilot.pause(0.5)
        if not any(w.is_running for w in app.workers):
            return
    raise TimeoutError

async def main():
    args = pm.parse_args([])
    s = pm.Settings(args.repo, args.author, args.base, args.jira_base, pm.core.DEFAULT_REPO_PATH,
                    14, pm.lup.resolve_token(None))
    app = pm.PRManager(s, pm.PlanOptions())
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_idle(app, pilot)
        print("PRs:", [r.number for r in app.rows])
        print("status:", app.query_one("#pr-status").render())
        # highlight #8704 and analyse
        idx = [r.number for r in app.rows].index(8704)
        app.query_one("#pr-table").move_cursor(row=idx)
        await pilot.press("a"); await wait_idle(app, pilot)
        print("tab:", app.tab)
        print(str(app.query_one("#analysis-text").render())[:600])
        await pilot.press("d"); await wait_idle(app, pilot)
        print("tab:", app.tab, "plan:", [(i.change.kind, i.change.headline) for i in app.plan])
        print(str(app.query_one("#plan-notes").render())[:500])
        # enter on first change -> confirm dialog, answer n
        app.query_one("#plan-table").focus()
        app.query_one("#plan-table").move_cursor(row=len(app.plan) - 1)
        await pilot.pause(0.3)
        await pilot.press("enter"); await pilot.pause(0.5)
        print(str(app.screen.query_one("#dialog-body Static").render())[:300])
        print("screen:", type(app.screen).__name__)
        await pilot.press("D"); await pilot.pause(0.5)
        print("after D in dialog:", type(app.screen).__name__, "running:", [w.group for w in app.workers if w.is_running])
        await pilot.press("n"); await pilot.pause(0.5)
        print("after n:", type(app.screen).__name__, app.plan[-1].state, "writes:", calls)
        # add a link by hand, then confirm with y (stubbed)
        await pilot.press("l"); await pilot.pause(0.5)
        print("screen:", type(app.screen).__name__)
        for ch in "HADOOP-19999": await pilot.press(ch if ch != "-" else "minus")
        await pilot.press("enter"); await pilot.pause(0.5)
        print("plan last:", app.plan[-1].change.headline)
        await pilot.press("enter"); await pilot.pause(0.5)
        print("screen:", type(app.screen).__name__)
        await pilot.press("y"); await wait_idle(app, pilot)
        print("state:", app.plan[-1].state, app.plan[-1].message, "writes:", len(calls))
        app.save_screenshot("tui_deps.svg", tempfile.gettempdir())
        # branches tab
        app.query_one("#tabs").active = "prs"; await pilot.pause(0.5)
        app.save_screenshot("tui_prs.svg", tempfile.gettempdir())
        app.query_one("#tabs").active = "branches"
        await pilot.pause(1); await wait_idle(app, pilot)
        print("branches:", len(app.branches), app.query_one("#branch-status").render())
asyncio.run(main())
