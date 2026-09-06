extends CharacterBody2D
class_name VoidPlayer

# --- Movement Parameters ---
@export_group("Movement")
@export var max_speed: float = 320.0
@export var acceleration: float = 1800.0
@export var friction: float = 2200.0
@export var air_acceleration: float = 1200.0

# --- Jump & Gravity ---
@export_group("Jump")
@export var jump_velocity: float = -520.0
@export var pogo_velocity: float = -480.0
@export var gravity_scale: float = 1.0
@export var fall_gravity_scale: float = 1.5
@export var coyote_time: float = 0.15
@export var jump_buffer_time: float = 0.12
@export var has_double_jump: bool = false
@export var max_jumps: int = 1
var jumps_left: int = 1

# --- Wall Mechanics ---
@export_group("Wall Mechanics")
@export var wall_slide_speed: float = 120.0
@export var wall_jump_velocity: Vector2 = Vector2(380.0, -480.0)

# --- Dash ---
@export_group("Dash")
@export var dash_speed: float = 750.0
@export var dash_duration: float = 0.18
@export var dash_cooldown: float = 0.5

# --- Combat & Health (Loop 2 & Loop 3) ---
@export_group("Combat & Health")
@export var max_health: int = 5
@export var current_health: int = 5
@export var max_soul: float = 100.0
@export var current_soul: float = 0.0
@export var attack_cooldown: float = 0.25
@export var attack_duration: float = 0.15

# --- State Variables ---
var coyote_timer: float = 0.0
var jump_buffer_timer: float = 0.0
var dash_cooldown_timer: float = 0.0
var dash_timer: float = 0.0
var attack_cooldown_timer: float = 0.0
var attack_timer: float = 0.0
var focus_heal_timer: float = 0.0

var is_dashing: bool = false
var is_attacking: bool = false
var is_focusing: bool = false
var attack_direction: Vector2 = Vector2.RIGHT
var facing_direction: int = 1

var inventory_keys: Array[String] = []

# Gravity reference
var gravity: float = ProjectSettings.get_setting("physics/2d/default_gravity", 980.0)

# Node references
@onready var slash_effect: ColorRect = $SlashVisual if has_node("SlashVisual") else null

signal health_changed(new_hp, max_hp)
signal soul_changed(new_soul, max_soul)

func _physics_process(delta: float) -> void:
	# Update timers
	_update_timers(delta)
	
	# Handle Focus Healing (Hold Q / A key)
	if Input.is_key_pressed(KEY_Q) or Input.is_key_pressed(KEY_A):
		_handle_focus_healing(delta)
	else:
		is_focusing = false
		focus_heal_timer = 0.0

	# Handle Dash Movement
	if is_dashing:
		velocity.x = facing_direction * dash_speed
		velocity.y = 0.0
		move_and_slide()
		return

	# Handle Horizontal Input
	var input_dir := Input.get_axis("ui_left", "ui_right")
	if input_dir != 0:
		facing_direction = 1 if input_dir > 0 else -1

	# Ground check & Coyote Time
	if is_on_floor():
		coyote_timer = coyote_time
	else:
		coyote_timer -= delta

	# Jump Buffering
	if Input.is_action_just_pressed("ui_accept"):
		jump_buffer_timer = jump_buffer_time
	else:
		jump_buffer_timer -= delta

	# Gravity Application
	var current_gravity = gravity * (fall_gravity_scale if velocity.y > 0 else gravity_scale)
	if not is_on_floor():
		velocity.y += current_gravity * delta

	# Wall Slide
	var is_on_wall = is_on_wall_only() and input_dir != 0
	if is_on_wall and velocity.y > 0:
		velocity.y = min(velocity.y, wall_slide_speed)

	# Jump Execution (Normal, Coyote, Wall Jump & Double Jump)
	if jump_buffer_timer > 0.0 and coyote_timer > 0.0:
		_execute_jump()
	elif jump_buffer_timer > 0.0 and is_on_wall:
		_execute_wall_jump(input_dir)
	elif jump_buffer_timer > 0.0 and not is_on_floor() and jumps_left > 0:
		_execute_jump()

	# Variable Jump Height (Cut jump short if released)
	if Input.is_action_just_released("ui_accept") and velocity.y < 0.0:
		velocity.y *= 0.45

	# Horizontal Acceleration & Friction
	var accel = acceleration if is_on_floor() else air_acceleration
	if input_dir != 0:
		velocity.x = move_toward(velocity.x, input_dir * max_speed, accel * delta)
	else:
		velocity.x = move_toward(velocity.x, 0.0, friction * delta)

	# Dash Execution
	if Input.is_key_pressed(KEY_SHIFT) or Input.is_action_just_pressed("ui_focus_next"):
		if dash_cooldown_timer <= 0.0 and not is_dashing:
			_start_dash()

	# Attack Execution (Loop 2: 4-Directional Slash / Loop 3: Down Attack Pogo)
	if (Input.is_key_pressed(KEY_J) or Input.is_key_pressed(KEY_Z) or Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT)):
		if attack_cooldown_timer <= 0.0 and not is_attacking:
			_execute_attack()

	move_and_slide()

func _execute_jump() -> void:
	velocity.y = jump_velocity
	coyote_timer = 0.0
	jump_buffer_timer = 0.0
	jumps_left -= 1

func _execute_wall_jump(input_dir: float) -> void:
	var wall_dir = get_wall_normal().x
	velocity.x = wall_dir * wall_jump_velocity.x
	velocity.y = wall_jump_velocity.y
	jump_buffer_timer = 0.0

func _start_dash() -> void:
	is_dashing = true
	dash_timer = dash_duration
	dash_cooldown_timer = dash_cooldown

func _execute_attack() -> void:
	is_attacking = true
	attack_timer = attack_duration
	attack_cooldown_timer = attack_cooldown
	
	# Determine Direction (Up, Down in air, or Facing Forward)
	var up_pressed = Input.is_key_pressed(KEY_W) or Input.is_action_pressed("ui_up")
	var down_pressed = Input.is_key_pressed(KEY_S) or Input.is_action_pressed("ui_down")
	
	if up_pressed:
		attack_direction = Vector2.UP
	elif down_pressed and not is_on_floor():
		attack_direction = Vector2.DOWN
		# Trigger Pogo Jump if hitting ground/hazard below!
		_trigger_pogo_bounce()
	else:
		attack_direction = Vector2.RIGHT if facing_direction > 0 else Vector2.LEFT
		
	_visualize_slash(attack_direction)
	_perform_attack_hit_check(attack_direction)
	# Add Soul on attack hit (Loop 3 mechanic)
	add_soul(11.0)

func _perform_attack_hit_check(dir: Vector2) -> void:
	var space_state = get_world_2d().direct_space_state
	var query = PhysicsShapeQueryParameters2D.new()
	var box = RectangleShape2D.new()
	if dir == Vector2.UP:
		box.size = Vector2(48, 30)
		query.transform = Transform2D(0, global_position + Vector2(0, -60))
	elif dir == Vector2.DOWN:
		box.size = Vector2(48, 30)
		query.transform = Transform2D(0, global_position + Vector2(0, 10))
	else:
		box.size = Vector2(36, 40)
		query.transform = Transform2D(0, global_position + Vector2(28 * facing_direction, -24))
	query.shape = box
	query.collide_with_bodies = true
	query.collide_with_areas = true
	query.collision_mask = 0xFFFFFFFF
	
	var results = space_state.intersect_shape(query, 16)
	for res in results:
		var collider = res.get("collider")
		if collider and collider != self:
			if collider.has_method("take_damage"):
				collider.take_damage(1, global_position)
			elif collider.get_parent() and collider.get_parent().has_method("take_damage"):
				collider.get_parent().take_damage(1, global_position)

func add_key(key_id: String) -> void:
	if not inventory_keys.has(key_id):
		inventory_keys.append(key_id)

func has_key(key_id: String) -> bool:
	return inventory_keys.has(key_id)

func _trigger_pogo_bounce() -> void:
	velocity.y = pogo_velocity
	coyote_timer = coyote_time

func add_soul(amount: float) -> void:
	current_soul = min(max_soul, current_soul + amount)
	emit_signal("soul_changed", current_soul, max_soul)

func take_damage(amount: int, source_pos: Vector2 = Vector2.ZERO) -> void:
	current_health = max(0, current_health - amount)
	emit_signal("health_changed", current_health, max_health)
	trigger_camera_shake(10.0)
	trigger_hitstop(0.08)
	if source_pos != Vector2.ZERO:
		var knock_dir = (global_position - source_pos).normalized()
		velocity.x = knock_dir.x * 280.0
		velocity.y = -220.0

func trigger_hitstop(duration: float = 0.06) -> void:
	Engine.time_scale = 0.05
	await get_tree().create_timer(duration, true, false, true).timeout
	Engine.time_scale = 1.0

func trigger_camera_shake(intensity: float = 6.0) -> void:
	var cam = get_node_or_null("Camera2D")
	if cam and cam.has_method("apply_shake"):
		cam.apply_shake(intensity)

func _handle_focus_healing(delta: float) -> void:
	if current_health >= max_health or current_soul < 33.0:
		return
	is_focusing = true
	focus_heal_timer += delta
	if focus_heal_timer >= 0.8: # Hold for 0.8s to heal 1 HP
		current_soul -= 33.0
		current_health = min(max_health, current_health + 1)
		focus_heal_timer = 0.0
		emit_signal("health_changed", current_health, max_health)
		emit_signal("soul_changed", current_soul, max_soul)

func _visualize_slash(dir: Vector2) -> void:
	if slash_effect:
		slash_effect.visible = true
		if dir == Vector2.UP:
			slash_effect.position = Vector2(-24, -80)
			slash_effect.size = Vector2(48, 20)
		elif dir == Vector2.DOWN:
			slash_effect.position = Vector2(-24, 0)
			slash_effect.size = Vector2(48, 20)
		else:
			slash_effect.position = Vector2(16 if dir.x > 0 else -40, -36)
			slash_effect.size = Vector2(24, 32)

func _update_timers(delta: float) -> void:
	if dash_cooldown_timer > 0.0:
		dash_cooldown_timer -= delta
	if attack_cooldown_timer > 0.0:
		attack_cooldown_timer -= delta
	if is_dashing:
		dash_timer -= delta
		if dash_timer <= 0.0:
			is_dashing = false
	if is_attacking:
		attack_timer -= delta
		if attack_timer <= 0.0:
			is_attacking = false
			if slash_effect:
				slash_effect.visible = false
