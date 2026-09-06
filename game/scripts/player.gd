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
@export var gravity_scale: float = 1.0
@export var fall_gravity_scale: float = 1.5
@export var coyote_time: float = 0.15
@export var jump_buffer_time: float = 0.12

# --- Wall Mechanics ---
@export_group("Wall Mechanics")
@export var wall_slide_speed: float = 120.0
@export var wall_jump_velocity: Vector2 = Vector2(380.0, -480.0)

# --- Dash ---
@export_group("Dash")
@export var dash_speed: float = 750.0
@export var dash_duration: float = 0.18
@export var dash_cooldown: float = 0.5

# --- State Timers ---
var coyote_timer: float = 0.0
var jump_buffer_timer: float = 0.0
var dash_cooldown_timer: float = 0.0
var dash_timer: float = 0.0
var is_dashing: bool = false
var facing_direction: int = 1

# Gravity reference
var gravity: float = ProjectSettings.get_setting("physics/2d/default_gravity", 980.0)

@onready var sprite: ColorRect = $ColorRect if has_node("ColorRect") else null

func _physics_process(delta: float) -> void:
	# Update timers
	_update_timers(delta)
	
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

	# Jump Execution (Normal & Coyote)
	if jump_buffer_timer > 0.0 and coyote_timer > 0.0:
		_execute_jump()
	elif jump_buffer_timer > 0.0 and is_on_wall:
		_execute_wall_jump(input_dir)

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

	move_and_slide()

func _execute_jump() -> void:
	velocity.y = jump_velocity
	coyote_timer = 0.0
	jump_buffer_timer = 0.0

func _execute_wall_jump(input_dir: float) -> void:
	var wall_dir = get_wall_normal().x
	velocity.x = wall_dir * wall_jump_velocity.x
	velocity.y = wall_jump_velocity.y
	jump_buffer_timer = 0.0

func _start_dash() -> void:
	is_dashing = true
	dash_timer = dash_duration
	dash_cooldown_timer = dash_cooldown

func _update_timers(delta: float) -> void:
	if dash_cooldown_timer > 0.0:
		dash_cooldown_timer -= delta
	if is_dashing:
		dash_timer -= delta
		if dash_timer <= 0.0:
			is_dashing = false
