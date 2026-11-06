import asyncio
import tempfile
import unittest

from bili_agent.model import Model
from bili_agent.store import Store


class ModelPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_replies_overtake_waiting_background_calls_and_cancellation_releases_queue(self):
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        class Peer:
            async def call(self, method, params, timeout):
                order.append(params['text'])
                if params['text'] == 'running':
                    entered.set()
                    await release.wait()
                return {'text': 'ok'}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            model = Model(Peer(), store)
            tasks = []
            try:
                tasks.append(asyncio.create_task(model.generate('segment_summary', '', 'running')))
                await entered.wait()
                tasks.append(asyncio.create_task(model.generate('segment_summary', '', 'background')))
                tasks.append(asyncio.create_task(model.generate('dm_reply', '', 'reply')))
                cancelled = asyncio.create_task(model.generate('segment_summary', '', 'cancelled'))
                await asyncio.sleep(0)
                cancelled.cancel()
                await asyncio.gather(cancelled, return_exceptions=True)
                await asyncio.wait_for(tasks[-1], 1)
                self.assertFalse(tasks[0].done())  # Chat completes while video analysis is still blocked.
                release.set()
                await asyncio.gather(*tasks)
                self.assertEqual(order, ['running', 'reply', 'background'])
                self.assertEqual(model.waiters, [])
                self.assertFalse(model.active)
            finally:
                release.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                store.close()
