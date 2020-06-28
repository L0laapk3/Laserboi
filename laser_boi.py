from config import *

from threading import Thread
from typing import List

from rlbot.agents.base_script import BaseScript
from rlbot.utils.game_state_util import CarState, GameState, BallState, Physics, Vector3
from rlbot.utils.structures.game_data_struct import PlayerInfo
from time import sleep

from rlbot_action_server.bot_action_broker import BotActionBroker, run_action_server, find_usable_port
from rlbot_action_server.bot_holder import set_bot_action_broker
from rlbot_action_server.models import BotAction, AvailableActions, ActionChoice, ApiResponse
from rlbot_action_server.formatting_utils import highlight_player_name
from rlbot_twitch_broker_client import Configuration, RegisterApi, ApiClient, ActionServerRegistration
from rlbot_twitch_broker_client.defaults import STANDARD_TWITCH_BROKER_PORT
from urllib3.exceptions import MaxRetryError

from dataclasses import dataclass
from rlutilities.linear_algebra import euler_to_rotation, dot, transpose, look_at, vec2, vec3, norm, normalize, angle_between, orthogonalize, project
from rlutilities.simulation import Ball, Field, Game, Car, ray as Ray
from rlutilities.mechanics import ReorientML
import math, fastrand

PUSH_STRENGTH_BALL = BASE_PUSH_STRENGTH * 4
PUSH_STRENGTH_BALL_ANGULAR = BASE_PUSH_STRENGTH * 20
PUSH_STRENGTH_CAR = BASE_PUSH_STRENGTH * 3
PUSH_STRENGTH_CAR_ANGULAR = BASE_PUSH_STRENGTH * 0.85

SET_LASER_BOI = 'setLaserBoi'
PLAYER_NAME = 'playerName'

@dataclass
class Push:
	velocity: vec3
	angular_velocity: vec3
	def __init__(self):
		self.velocity = vec3(0, 0, 0)
		self.angular_velocity = vec3(0, 0, 0)

@dataclass
class Laser:
	laserType: int
	time_remaining: float

def toVector3(v: vec3):
	return Vector3(v[0], v[1], v[2])


if TWITCH_CHAT_INTERACTION:
	class MyActionBroker(BotActionBroker):
		def __init__(self, script):
			self.script = script

		def get_actions_currently_available(self) -> List[AvailableActions]:
			return self.script.get_actions_currently_available()

		def set_action(self, choice: ActionChoice):
			self.script.process_choice(choice.action)
			return ApiResponse(200, f"{choice.action.description}")



import timeit


class Laserboi(BaseScript):

	def __init__(self):
		super().__init__("Laser_boi")
		if TWITCH_CHAT_INTERACTION:
			self.action_broker = MyActionBroker(self)
		self.known_players: List[PlayerInfo] = []
		self.game = Game()
		self.game.set_mode("soccar")
		self.car_lasers = { }
		self.last_seconds_elapsed = 0
		self.forces = {}
		self.lastScore = 0
		self.isPaused = False
		self.boostContent = {}
		self.boost = {}

		self.lastFullSecond = 0
		self.ticksThisSecond = 0

	def heartbeat_connection_attempts_to_twitch_broker(self, port):
		if TWITCH_CHAT_INTERACTION:
			register_api_config = Configuration()
			register_api_config.host = f"http://127.0.0.1:{STANDARD_TWITCH_BROKER_PORT}"
			twitch_broker_register = RegisterApi(ApiClient(configuration=register_api_config))
			while True:
				print("shit is running!")
				try:
					twitch_broker_register.register_action_server(
						ActionServerRegistration(base_url=f"http://127.0.0.1:{port}"))
				except MaxRetryError:
					self.logger.warning('Failed to register with twitch broker, will try again...')
				sleep(10)

	def process_choice(self, choice: BotAction):
		if TWITCH_CHAT_INTERACTION:
			if choice.action_type != SET_LASER_BOI:
				return

			player_index = self.get_player_index_by_name(choice.data[PLAYER_NAME])
			if player_index is None:
				return

			if not ALLOW_MULTIPLE_AT_ONCE:
				self.car_lasers.clear()
			self.car_lasers[player_index] = Laser(0, LASER_DURATION)


	def start(self):
		
		if TWITCH_CHAT_INTERACTION:
			port = find_usable_port(9097)
			Thread(target=run_action_server, args=(port,), daemon=True).start()
			set_bot_action_broker(self.action_broker)  # This seems to only work after the bot hot reloads once, weird.

			Thread(target=self.heartbeat_connection_attempts_to_twitch_broker, args=(port,), daemon=True).start()

		while True:
			sleep(0)
			packet = self.wait_game_tick_packet()
			raw_players = [self.game_tick_packet.game_cars[i]
						   for i in range(packet.num_cars)]
			self.known_players = [p for p in raw_players if p.name]
			if self.last_seconds_elapsed == packet.game_info.seconds_elapsed:
				continue
			elapsed_now = packet.game_info.seconds_elapsed - self.last_seconds_elapsed
			self.last_seconds_elapsed = packet.game_info.seconds_elapsed

			self.ticksThisSecond += 1
			if int(packet.game_info.seconds_elapsed) != self.lastFullSecond:
				print("ticks this second:", self.ticksThisSecond)
				self.ticksThisSecond = 0
				self.lastFullSecond = int(packet.game_info.seconds_elapsed)
			
			self.game.read_game_information(packet, None)

			for v in self.car_lasers.values():
				print(v)
				v.time_remaining -= elapsed_now

			if TWITCH_CHAT_INTERACTION:
				self.car_lasers = {k:v for k, v in self.car_lasers.items() if v.time_remaining >= 0}
			else:
				self.car_lasers = {}
				for i in range(self.game.num_cars):
					self.car_lasers[i] = Laser(0, math.inf)

			if packet.teams[0].score - packet.teams[1].score != self.lastScore:
				self.isPaused = True
				self.lastScore = packet.teams[0].score - packet.teams[1].score
			elif self.game.ball.position[0] == 0 and self.game.ball.position[1] == 0 and self.game.ball.velocity[0] == 0 and self.game.ball.velocity[1] == 0:
				self.isPaused = False
			
			ballTouchers = []
			fastrand.pcg32_seed(int(packet.game_info.seconds_elapsed / .14))

			if DURING_BOOST_ONLY:
				boosting = {}
				boostContent = {}
				for i in range(self.game.num_cars):
					car = self.game.cars[i]
					boosting[i] = i in self.boostContent and (3 if self.boostContent[i] > car.boost or (self.boostContent[i] < car.boost and self.boosting[i]) else max(0, self.boosting[i] - 1))
					boostContent[i] = car.boost
				self.boosting = boosting
				self.boostContent = boostContent

			for index in range(self.game.num_cars):
				car = self.game.cars[index]

				self.renderer.begin_rendering(str(index) + "Lb")
				
				if index in self.car_lasers and not packet.game_cars[index].is_demolished and (not DURING_BOOST_ONLY or self.boosting[index]):# and not self.isPaused:
					for leftRight in (-1, 1):
						startPoint = car.position + car.forward() * 63 + leftRight * car.left() * 26 + car.up() * 3
						direction = normalize(orthogonalize(car.forward(), vec3(0, 0, 1))) if car.on_ground and abs(dot(car.up(), vec3(0, 0, 1))) > 0.999 else car.forward()

						for bounce in range(1):
							closest = math.inf
							closestTarget = None
							toBall = self.game.ball.position - car.position
							toBallProj = project(toBall, direction)
							toBallOrth = toBall - toBallProj
							toCollisionOrth = toBallOrth
							endVector = direction
							if norm(toBallOrth) <= self.game.ball.radius and dot(toBallProj, direction) > 0:
								closestTarget = -1
								closest = norm(toBallProj) - math.sqrt(self.game.ball.radius**2 - norm(toBallOrth)**2)
								ballTouchers.append(index)

							for otherIndex in range(self.game.num_cars):
								if otherIndex == index:
									continue
								otherCar = self.game.cars[otherIndex]
								
								v_local = dot(startPoint - otherCar.hitbox().center + 15 * otherCar.up(), otherCar.hitbox().orientation)
								d_local = dot(direction, otherCar.hitbox().orientation)
								def lineFaceCollision(i):
									offset = vec3(0, 0, 0)
									offset[i] = math.copysign(otherCar.hitbox().half_width[i], -d_local[i])
									collisionPoint = v_local - offset
									try:
										distance = -collisionPoint[i] / d_local[i]
									except ZeroDivisionError:
										return None
									if distance < 0:
										return None
									collisionPoint += d_local * distance
									for j in range(i == 0, 3 - (i == 2), 1 + (i == 1)):
										if abs(collisionPoint[j]) > otherCar.hitbox().half_width[j]:
											return None
									collisionPoint[i] = offset[i]
									# print(dot(otherCar.hitbox().orientation, collisionPoint) + otherCar.hitbox().center - 15 * otherCar.up())
									return distance
								distance = lineFaceCollision(0) or lineFaceCollision(1) or lineFaceCollision(2)
								if distance is not None:
									# collisionPoint = dot(otherCar.hitbox().orientation, collisionPoint) + otherCar.hitbox().center
									collisionPoint = startPoint + distance * direction
									toCollisionOrth = orthogonalize(collisionPoint - startPoint, direction)
									if distance < closest:
										closest = distance
										closestTarget = otherIndex


							if closestTarget is not None:
								if closestTarget not in self.forces:
									self.forces[closestTarget] = Push()
								self.forces[closestTarget].velocity += direction * elapsed_now
								self.forces[closestTarget].angular_velocity += toCollisionOrth * -1 * direction / norm(toCollisionOrth)**2 * elapsed_now
								pass
							else:
								# simulate raycast closest
								length = 100000
								ray = Ray(startPoint, direction * length)
								while closest >= length + .2:
									closest = length
									newStartPoint, mirrorDirection = ray.start, ray.direction
									ray = Field.raycast_any(Ray(startPoint, direction * (length - .1)))
									length = norm(ray.start - startPoint)
								newDirection = direction - 2 * dot(direction, mirrorDirection) * mirrorDirection
								endVector = direction * 0.6 - mirrorDirection * 0.4
							
							R = 4
							COLORSPIN = 2
							SCATTERSPIN = 0.75
							for i in range(LASERLINES):
								i = i / LASERLINES * 2 * math.pi
								offset = dot(look_at(direction, vec3(0, 0, 1)), vec3(0, R * math.sin(i), R * math.cos(i)))
								color = self.renderer.create_color(255, 
									int(255 * (0.5 + 0.5 * math.sin(packet.game_cars[index].physics.rotation.roll + leftRight * i + (COLORSPIN * packet.game_info.seconds_elapsed)))),
									int(255 * (0.5 + 0.5 * math.sin(packet.game_cars[index].physics.rotation.roll + leftRight * i + (COLORSPIN * packet.game_info.seconds_elapsed + 2 / 3 * math.pi)))),
									int(255 * (0.5 + 0.5 * math.sin(packet.game_cars[index].physics.rotation.roll + leftRight * i + (COLORSPIN * packet.game_info.seconds_elapsed + 4 / 3 * math.pi))))
								)
								start_time = timeit.default_timer()
								self.renderer.draw_line_3d(startPoint + offset, startPoint + offset + closest * direction, color)
							
							for _ in range(SCATTERLINES):
								r = fastrand.pcg32bounded(int(2 * math.pi * 2**10)) / 2**10
								c = leftRight * r - (SCATTERSPIN - COLORSPIN) * packet.game_info.seconds_elapsed
								i = packet.game_cars[index].physics.rotation.roll + r - leftRight * (SCATTERSPIN) * packet.game_info.seconds_elapsed
								# c = random.uniform(0, 2 * math.pi)
								color = self.renderer.create_color(255, 
									int(255 * (0.5 + 0.5 * math.sin(c))),
									int(255 * (0.5 + 0.5 * math.sin(c + 2 / 3 * math.pi))),
									int(255 * (0.5 + 0.5 * math.sin(c + 4 / 3 * math.pi)))
								)
								length = 15 * math.exp(-fastrand.pcg32bounded(2**10) / 2**10)
								scatterStart = startPoint + closest * direction + dot(look_at(direction, vec3(0, 0, 1)), vec3(0, R * math.sin(i), R * math.cos(i)))
								scatterEnd = scatterStart + dot(look_at(endVector, vec3(0, 0, 1)), vec3(-length, length * math.sin(i), length * math.cos(i)))
								start_time = timeit.default_timer()
								self.renderer.draw_line_3d(scatterStart, scatterEnd, color)
							

							if closestTarget is not None:
								break
							else:
								startPoint, direction = newStartPoint + 0.1 * newDirection, newDirection

				self.renderer.end_rendering()
				self.lastBallPos = self.game.ball.position

			ballState = None
			if -1 in self.forces:
				ballState = BallState(
					# latest_touch=Touch(player_name=packet.game_cars[random.choice(ballTouchers)].name),
					physics=Physics(
						velocity=toVector3(self.game.ball.velocity + self.forces[-1].velocity * PUSH_STRENGTH_BALL),
						angular_velocity=toVector3(self.game.ball.angular_velocity + self.forces[-1].angular_velocity * PUSH_STRENGTH_BALL_ANGULAR)
					)
				)
				del self.forces[-1]
			carStates = {}
			for i, force in self.forces.items():
				carStates[i] = CarState(physics=Physics(
					velocity=toVector3(self.game.cars[i].velocity + self.forces[i].velocity * PUSH_STRENGTH_CAR),
					angular_velocity=toVector3(self.game.cars[i].angular_velocity + self.forces[i].angular_velocity * PUSH_STRENGTH_CAR_ANGULAR)
				))
			self.forces.clear()
			self.set_game_state(GameState(cars=carStates, ball=ballState))
			
				
			

	def get_player_index_by_name(self, name: str):
		for i in range(self.game_tick_packet.num_cars):
			car = self.game_tick_packet.game_cars[i]
			if car.name == name:
				return i
		return None

	def get_actions_currently_available(self) -> List[AvailableActions]:
		actions = []
		for player in self.known_players:
			actions.append(BotAction(description=f'Make {highlight_player_name(player)} the laser boi',
									 action_type=SET_LASER_BOI,
									 data={PLAYER_NAME: player.name}))
		return [AvailableActions("Laserboi", None, actions)]


if __name__ == '__main__':
	laserboi = Laserboi()
	laserboi.start()



