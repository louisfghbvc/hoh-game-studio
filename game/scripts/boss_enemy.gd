extends VoidEnemy
class_name GuardianBoss

@export var phase2_health_threshold: int = 10
@export var charge_speed: float = 180.0
@export var jump_slam_force: Vector2 = Vector2(200.0, -500.0)
@export var projectile_scene: PackedScene = preload("res://scenes/boss_projectile.tscn")

enum State { IDLE, CHARGING, JUMP_SLAM, RANGED_ATTACK, STUNNED }
var current_state: State = State.IDLE

var state_timer: float = 0.0
var move_direction: int = 1
var is_phase_2: bool = false
var gravity: float = ProjectSettings.get_setting("physics/2d/default_gravity", 980.0)

signal boss_defeated

func _ready() -> void:
	max_health = 20
	current_health = 20
	contact_damage = 2
	soul_reward = 100.0
	super._ready()

func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity.y += gravity * delta

	state_timer -= delta
	if state_timer <= 0.0:
		_choose_next_state()

	match current_state:
		State.IDLE:
			velocity.x = move_toward(velocity.x, 0.0, 500.0 * delta)
		State.CHARGING:
			velocity.x = move_direction * charge_speed
			if is_on_wall():
				move_direction *= -1
		State.JUMP_SLAM:
			if is_on_floor() and velocity.y >= 0:
				_on_slam_land()
		State.RANGED_ATTACK:
			velocity.x = 0.0
		State.STUNNED:
			velocity.x = 0.0

	move_and_slide()

func _choose_next_state() -> void:
	if current_health <= phase2_health_threshold:
		is_phase_2 = true

	var player = get_tree().get_first_node_in_group("player")
	if player:
		move_direction = 1 if player.global_position.x > global_position.x else -1

	var choice = randf()
	if is_phase_2:
		if choice < 0.4:
			_start_ranged_attack()
		elif choice < 0.7:
			_start_jump_slam()
		else:
			current_state = State.CHARGING
			state_timer = 2.0
	else:
		if choice < 0.3:
			_start_ranged_attack()
		else:
			current_state = State.CHARGING
			state_timer = randf_range(1.5, 3.0)

func _start_jump_slam() -> void:
	current_state = State.JUMP_SLAM
	velocity.x = move_direction * jump_slam_force.x
	velocity.y = jump_slam_force.y
	state_timer = 2.0

func _start_ranged_attack() -> void:
	current_state = State.RANGED_ATTACK
	state_timer = 1.2
	_fire_projectiles()

func _fire_projectiles() -> void:
	if not projectile_scene:
		return
	
	var base_dir = Vector2.RIGHT if move_direction > 0 else Vector2.LEFT
	if is_phase_2:
		# 3-bullet spread attack
		for angle in [-20.0, 0.0, 20.0]:
			var proj = projectile_scene.instantiate()
			get_parent().add_child(proj)
			proj.global_position = global_position + Vector2(0, -20)
			proj.set_direction(base_dir.rotated(deg_to_rad(angle)))
	else:
		# Single projectile attack
		var proj = projectile_scene.instantiate()
		get_parent().add_child(proj)
		proj.global_position = global_position + Vector2(0, -20)
		proj.set_direction(base_dir)

func _on_slam_land() -> void:
	current_state = State.IDLE
	state_timer = 1.0

func die() -> void:
	emit_signal("boss_defeated")
	super.die()
