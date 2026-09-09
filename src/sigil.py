import asyncio
from wizwalker import Keycode, Client, XYZ, MemoryReadError
from loguru import logger
from src.teleport_math import navmap_tp, collision_tp, calc_FrontalVector, are_xyzs_within_threshold
from src.utils import is_visible_by_path, click_window_by_path, wait_for_zone_change, auto_potions, logout_and_in, is_free, get_quest_name, collect_wisps
from src.paths import team_up_button_path, team_up_confirm_path, dungeon_warning_path, cancel_chest_roll_path, npc_range_path
from src.sprinty_client import SprintyClient
from typing import List


class Sigil():
	def __init__(self, client: Client, clients: list[Client], leader_pid: int):
		self.client = client
		self.clients = clients
		self.leader_pid = leader_pid


	async def record_sigil(self):
		self.sigil_xyz = await self.client.body.position()
		self.sigil_zone = await self.client.zone_name()
		# mark location
		# await self.client.send_key(Keycode.PAGE_DOWN, 0.1)


	async def record_quest(self):
		self.original_quest = await get_quest_name(self.client)

	async def quest_in_progress(self, client: Client = None) -> bool:
		# True only when a tracked quest is actively advancing (not turned in
		# and not absent). Distinguishes a real questline dungeon from a
		# no-quest / post-final-boss sigil farm so the farm re-enters the same
		# sigil instead of chase-teleporting to whatever the journal shows.
		if not client:
			client = self.client
		try:
			qid = await client.quest_id()
			if qid == 0:
				return False
			quests = await (await client.quest_manager()).quest_data()
			if qid not in quests:
				# Tracked quest id transiently missing from quest_data() (e.g.
				# mid-transition): treat as not in progress. Deliberately
				# conservative - safer to re-enter the farm sigil than to chase
				# a quest marker that may have just moved on.
				return False
			return not await quests[qid].ready_to_turn_in()
		except (ValueError, MemoryReadError):
			return False

	@logger.catch()
	async def team_up(self, client: Client = None):
		# Wait for team up to be visible in case it isnt
		if not client:
			client = self.client
		while not await is_visible_by_path(client, team_up_button_path):
			await asyncio.sleep(0.25)
		# click team up button
		await click_window_by_path(client, team_up_button_path, True)
		await asyncio.sleep(0.5)
		if await is_visible_by_path(client, team_up_confirm_path):
			await click_window_by_path(client, team_up_confirm_path, True)
			while not await client.is_loading():
				await asyncio.sleep(0.1)

			await wait_for_zone_change(client, True)
		else:
			while not await client.is_loading():
				await asyncio.sleep(0.1)
			await wait_for_zone_change(client, True)

	@logger.catch()
	async def join_sigil(self, client: Client = None):
		if not client:
			client = self.client
		if client.use_team_up:
			await self.team_up(client)
		else:
			current_zone = await client.zone_name()
			await client.send_key(Keycode.X, seconds=0.1)
			await asyncio.sleep(0.5)
			if await is_visible_by_path(client, dungeon_warning_path):
				await client.send_key(Keycode.ENTER, 0.1)
			await wait_for_zone_change(client, current_zone=current_zone)

	async def _dismiss_chest_roll(self, client: Client):
		# Bounded cancel of the post-boss chest roll window. The roll renders
		# late and lingers across many free-loop ticks, so a single visibility
		# check is racy. is_free() ignores the roll window: if we hand control
		# back to the caller's logout_and_in tail with the roll still up, its
		# single ESC can be swallowed by the roll prompt and stall
		# wait_for_window_by_path(quit) forever. Poll briefly; if it never
		# shows, we're done.
		for _ in range(50):  # ~5s cap
			if not await is_visible_by_path(client, cancel_chest_roll_path):
				return
			await click_window_by_path(client, cancel_chest_roll_path)
			await asyncio.sleep(0.1)

	@logger.catch()
	async def rereenter_sigil(self, client: Client = None):
		# After the final boss of a no-quest sigil farm, walk back out to the
		# recorded sigil zone and re-enter the same sigil instead of chasing
		# the journal quest (which may be empty or point at a different sigil).
		if not client:
			client = self.client

		# The post-boss chest roll window is not covered by is_free(), so it can
		# still be up when we start the exit walk. Cancel it now; otherwise the
		# ESC that logout_and_in sends below can be swallowed by the roll prompt
		# and stall wait_for_window_by_path forever. (The free-loop path cancels
		# this on its own iterations, but the rereenter break skips that code.)
		# The roll renders late and lingers, so we re-check immediately before
		# every return/fall-through below as well, so logout_and_in's single ESC
		# is never swallowed mid-exit.
		await self._dismiss_chest_roll(client)

		# Walk back out to the sigil zone. Bounded (like the short-dungeon exit:
		# counter < 35) so a wrong facing / bad geometry never hangs the farm
		# pressing W forever. If it doesn't trigger a loading screen, teleport
		# back to the recorded sigil xyz and return so the caller's logout_and_in
		# tail resets us instead of looping unboundedly.
		await self.movement_checked_teleport(self.sigil_xyz, client=client)
		counter = 0
		while not await client.is_loading() and counter < 35:
			await client.send_key(Keycode.W, seconds=0.1)
			counter += 1
		if counter >= 35:
			await client.teleport(self.sigil_xyz)
			await self._dismiss_chest_roll(client)  # re-check before logout tail
			return

		# Bound the wait for the exit loading to settle back into the recorded
		# sigil zone. wait_for_zone_change(to_zone=...) spins forever if the
		# exit drops the wizard somewhere the recorded zone name never matches
		# (or the zone is briefly unreadable), so cap it like the W-walk above
		# and fall back to the caller's logout_and_in reset instead of hanging.
		zone_wait_counter = 0
		while (await client.zone_name() != self.sigil_zone or await client.is_loading()) and zone_wait_counter < 300:
			await asyncio.sleep(0.1)
			zone_wait_counter += 1
		if zone_wait_counter >= 300:
			# Never reached the sigil zone; return so the caller's logout_and_in
			# tail resets us (it waits for is_free first, so a still-loading
			# client is handled there rather than hung here).
			await self._dismiss_chest_roll(client)  # re-check before logout tail
			return
		# Re-check the roll right before the successful exit: a roll that popped
		# up during the walk must be dismissed so logout_and_in's single ESC
		# isn't swallowed by the roll prompt.
		await self._dismiss_chest_roll(client)
		# NOTE: deliberately no teleport + press A here. The caller's shared tail
		# (wait_free -> logout_and_in -> teleport to sigil_xyz + press A at the
		# end of solo/leader farming logic) already does the final teleport and
		# A press. Doing it here too would press A into the sigil/team-up prompt
		# *before* logout_and_in sends its single ESC, and the prompt can swallow
		# that ESC, stalling wait_for_window_by_path forever, or the A press is
		# just dead interaction. Let the tail own the entry.


	async def go_through_zone_changes(self):
		while await self.client.zone_name() != self.sigil_zone:
			# teleport to quest, will NOT work if the user has no quest up
			quest_xyz = await self.client.quest_position.position()
			await collision_tp(self.client, quest_xyz)

			# walk forward until we get a zone change
			while not await self.client.is_loading():
				await self.client.send_key(Keycode.W, seconds=0.1)
			await wait_for_zone_change(self.client, True)
			await asyncio.sleep(1)


	async def wait_for_combat_finish(self, await_combat: bool = True, should_collect_wisps: bool = True):
		if await_combat:
			while not await self.client.in_battle():
				await asyncio.sleep(0.1)
		while await self.client.in_battle():
			await asyncio.sleep(0.1)
		if should_collect_wisps:
			await collect_wisps(self.client)


	async def movement_checked_teleport(self, xyz: XYZ, client: Client = None):
		# Honour an explicit client so leader mode can teleport each follower
		# independently. Without this, every follower's rereenter_sigil call
		# re-fires the goto/teleport on self.client (the leader) while the
		# followers only get the unbounded W-walk.
		if not client:
			client = self.client
		current_xyz = await client.body.position()
		frontal_xyz = await calc_FrontalVector(client=client, speed_constant=200, speed_adjusted=False)
		await client.goto(frontal_xyz)
		if not await are_xyzs_within_threshold(current_xyz, await client.body.position(), threshold=20):
			await client.teleport(xyz)


	async def wait_for_sigil(self):
		# Waits for the client to walk near a sigil
		while self.client.sigil_status:
			await asyncio.sleep(0.25)
			if not await is_visible_by_path(self.client, team_up_button_path):
				pass

			else:
				await self.farm_sigil()

	@logger.catch()
	async def solo_farming_logic(self):
		while self.client.sigil_status:
			while not await is_visible_by_path(self.client, team_up_button_path) and self.client.sigil_status:
				await asyncio.sleep(0.1)

			# Automatically use and buy potions if needed
			if self.client.use_potions:
				await auto_potions(self.client, buy = self.client.buy_potions)

			# Join sigil and wait for the zone to change either via team up or sigil countdown
			await self.join_sigil()
			await asyncio.sleep(1.5)

			# if quest objective is same, we know it's a short dungeon, most likely with 1 room
			if await get_quest_name(self.client) == self.original_quest:
				start_xyz = await self.client.body.position() 
				second_xyz = await calc_FrontalVector(self.client, speed_constant=200, speed_adjusted=False)
				await asyncio.sleep(5.0)
				await SprintyClient(self.client).tp_to_closest_mob()
				await self.wait_for_combat_finish()
				await asyncio.sleep(0.1)

				after_xyz = await calc_FrontalVector(self.client, speed_constant=450, speed_adjusted=False)

				await collect_wisps(self.client)

				await self.client.teleport(after_xyz)
				await asyncio.sleep(0.1)
				while True:
					await self.client.goto(second_xyz.x, second_xyz.y)
					await asyncio.sleep(0.1)
					await self.client.goto(start_xyz.x, start_xyz.y)
					past_zone_change_xyz = await calc_FrontalVector(self.client, speed_adjusted=False)
					await self.client.goto(past_zone_change_xyz.x, past_zone_change_xyz.y)
					counter = 0
					while not await self.client.is_loading() and counter < 35:
						await asyncio.sleep(0.1)
						counter += 1
					if counter >= 35:
						await self.client.teleport(after_xyz)
						pass
					else:
						break

				logger.debug(f'Client {self.client.title} - Awaiting loading')
				while await self.client.is_loading():
					await asyncio.sleep(0.1)

			else:
				# TODO: Logic for dungeons with questlines
				while self.client.sigil_status:
					await asyncio.sleep(1)
					if await is_free(self.client):
						quest_xyz = await self.client.quest_position.position()
						if await get_quest_name(self.client) != self.original_quest:
							# Only chase the journal quest when a tracked quest is actively
							# advancing. After a no-quest farm's final boss (or with no quest
							# at all), re-enter the same sigil instead of teleporting to
							# whatever the journal now shows.
							if await self.quest_in_progress(self.client):
								try:
									await collision_tp(self.client, quest_xyz)
								except ValueError:
									pass
							else:
								await self.rereenter_sigil(self.client)
								break

						await asyncio.sleep(0.25)

						if await is_visible_by_path(self.client, cancel_chest_roll_path):
							await click_window_by_path(self.client, cancel_chest_roll_path)

						if await is_visible_by_path(self.client, npc_range_path):
							await self.client.send_key(Keycode.X, 0.1)

						if await get_quest_name(self.client) == self.original_quest:
							await asyncio.sleep(1)
							break

				while not await is_free(self.client) and self.client.sigil_status:
					await asyncio.sleep(0.1)
				await logout_and_in(self.client)

			while not await is_free(self.client) and self.client.sigil_status:
				await asyncio.sleep(0.1)

			if self.client.sigil_status:
				await asyncio.sleep(1)
				await self.client.teleport(self.sigil_xyz)
				await self.client.send_key(Keycode.A, 0.1)


	async def leader_farming_logic(self):
		self.follower_clients: List[Client] = []
		for client in self.clients:
			if client.process_id != self.leader_pid:
				self.follower_clients.append(client)

		while self.client.sigil_status:
			while not await is_visible_by_path(self.client, team_up_button_path) and self.client.sigil_status:
				await asyncio.sleep(0.1)

			# Automatically use and buy potions if needed
			if self.client.use_potions:
				for client in self.clients:
					await auto_potions(client, mark=True, buy = self.client.buy_potions)

			# await asyncio.gather(*[auto_potions(p) for p in self.clients])

			current_pos = await self.client.body.position()
			await asyncio.gather(*[p.teleport(current_pos) for p in self.follower_clients])

			# Join sigil and wait for the zone to change either via team up or sigil countdown
			await asyncio.gather(*[self.join_sigil(p) for p in self.clients])
			
			# if quest objective is same, we know it's a short dungeon, most likely with 1 room
			if await get_quest_name(self.client) == self.original_quest:
				start_xyz = await self.client.body.position() 
				second_xyz = await calc_FrontalVector(self.client, speed_constant=200, speed_adjusted=False)
				await asyncio.gather(*[SprintyClient(p).tp_to_closest_mob() for p in self.clients])

				await self.wait_for_combat_finish()

				await asyncio.sleep(0.1)

				after_xyz = await calc_FrontalVector(self.client, speed_constant=450, speed_adjusted=False)

				await asyncio.gather(*[collect_wisps(p) for p in self.clients])

				await asyncio.gather(*[p.teleport(after_xyz) for p in self.clients])
				await asyncio.sleep(0.1)

				async def exit_sigil(client: Client):
					while True:
						await client.goto(second_xyz.x, second_xyz.y)
						await asyncio.sleep(0.1)
						await client.goto(start_xyz.x, start_xyz.y)
						past_zone_change_xyz = await calc_FrontalVector(client, speed_adjusted=False)
						await client.goto(past_zone_change_xyz.x, past_zone_change_xyz.y)
						counter = 0
						while not await client.is_loading() and counter < 35:
							await asyncio.sleep(0.1)
							counter += 1
						if counter >= 35:
							await client.teleport(after_xyz)
							pass
						else:
							break

					logger.debug(f'Client {client.title} - Awaiting loading')
					while await client.is_loading():
						await asyncio.sleep(0.1)

				await asyncio.gather(*[exit_sigil(p) for p in self.clients])

			else:
				# TODO: Logic for dungeons with questlines
				while self.client.sigil_status:
					await asyncio.sleep(1)
					if await is_free(self.client):
						quest_xyz = await self.client.quest_position.position()
						if await get_quest_name(self.client) != self.original_quest:
							# Only chase the journal quest when a tracked quest is actively
							# advancing. After a no-quest farm's final boss (or with no quest
							# at all), re-enter the same sigil instead of teleporting to
							# whatever the journal now shows.
							if await self.quest_in_progress(self.client):
								try:
									# await navmap_tp(self.client, quest_xyz, auto_quest_leader=True)
									await asyncio.gather(*[collision_tp(p, quest_xyz, leader_client=self.client) for p in self.clients])
								except ValueError:
									pass
							else:
								await asyncio.gather(*[self.rereenter_sigil(p) for p in self.clients])
								break

						await asyncio.sleep(0.25)

						for client in self.clients:
							if await is_visible_by_path(client, cancel_chest_roll_path):
								await click_window_by_path(client, cancel_chest_roll_path)

						for client in self.clients:
							if await is_visible_by_path(self.client, npc_range_path):
								await self.client.send_key(Keycode.X, 0.1)

						if await get_quest_name(self.client) == self.original_quest:
							await asyncio.sleep(1)
							break

				while not await is_free(self.client) and self.client.sigil_status:
					await asyncio.sleep(0.1)

				await asyncio.gather(*[logout_and_in(p) for p in self.clients])

			while not await is_free(self.client) and self.client.sigil_status:
				await asyncio.sleep(0.1)

			if self.client.sigil_status:
				await asyncio.sleep(1)
				await asyncio.gather(*[p.teleport(self.sigil_xyz) for p in self.clients])
				await asyncio.gather(*[p.send_key(Keycode.A, 0.1) for p in self.clients])
				await self.client.teleport(self.sigil_xyz)


	async def follower_farming_logic(self):
		while self.client.sigil_status:
			await asyncio.sleep(0.1)


	async def farm_sigil(self):
		# Main loop for farming a sigil, includes setup
		logger.debug(f'Client {self.client.title} at sigil, farming it.')
		await self.record_sigil()
		await self.record_quest()

		if self.leader_pid:
			self.leader: Client = None

			# Determines what client the leader is
			for client in self.clients:
				if client.process_id == self.leader_pid:
					self.leader = client
					break

			if self.leader.process_id == self.client.process_id:
				await self.leader_farming_logic()

			else:
				# TODO: Somehow finish this function
				await self.follower_farming_logic()

		else:
			await self.solo_farming_logic()
