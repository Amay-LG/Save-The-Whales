import numpy as np
from dataclasses import dataclass
from typing import List, Optional

# --- 1. KINEMATICS & STATE REPRESENTATION ---

@dataclass
class Vector2D:
    x: float
    y: float
    
    def to_array(self):
        return np.array([self.x, self.y])

class Ship:
    def __init__(self, id: str, pos: Vector2D, vel: Vector2D, is_own_ship: bool = False):
        self.id = id
        self.pos = pos.to_array()
        self.vel = vel.to_array() # (vx, vy) based on Speed Over Ground & Heading
        self.is_own_ship = is_own_ship

    def project_position(self, t: float) -> np.ndarray:
        """P(t) = P0 + V * t"""
        return self.pos + (self.vel * t)

class Whale:
    def __init__(self, id: str, last_seen_pos: Vector2D, time_since_seen: float):
        self.id = id
        self.pos = last_seen_pos.to_array()
        self.time_since_seen = time_since_seen
        # Whales expand into a probability cloud the longer they are underwater
        self.max_swim_speed = 5.0 # knots (~2.5 m/s)

    def get_uncertainty_radius(self) -> float:
        """R(t) = R_0 + v_max * delta_t. The hazard zone grows over time."""
        base_radius = 200.0 # meters
        growth = self.max_swim_speed * self.time_since_seen 
        return base_radius + growth


# --- 2. THREAT DETECTION (Math 20C Vector Calculus) ---

def calculate_cpa(own_ship: Ship, target: Ship) -> tuple[float, float]:
    """
    Calculates Time to Closest Point of Approach (TCPA) and the CPA distance.
    Returns (tcpa_seconds, cpa_distance_meters).
    """
    p_rel = target.pos - own_ship.pos  # Relative position vector
    v_rel = target.vel - own_ship.vel  # Relative velocity vector
    
    v_rel_sq = np.dot(v_rel, v_rel)
    if v_rel_sq == 0: # Ships are moving exactly parallel at the same speed
        return 0.0, np.linalg.norm(p_rel)
        
    # t = -(p_rel dot v_rel) / |v_rel|^2
    tcpa = -np.dot(p_rel, v_rel) / v_rel_sq
    
    if tcpa < 0:
        # Closest point was in the past; they are moving away from each other
        return tcpa, np.linalg.norm(p_rel)
        
    # CPA = |P_rel + V_rel * t|
    cpa_pos = p_rel + (v_rel * tcpa)
    cpa_dist = np.linalg.norm(cpa_pos)
    
    return tcpa, cpa_dist


# --- 3. PATH GENERATION & EVALUATION (The MPC Cost Function) ---

class MaritimePathfinder:
    def __init__(self, safe_ship_distance: float, max_turn_deg: float):
        self.safe_ship_distance = safe_ship_distance
        self.max_turn = np.radians(max_turn_deg)
        self.active_command = None # Used for hysteresis (anti-thrashing)

    def generate_candidate_maneuvers(self, current_vel: np.ndarray) -> List[np.ndarray]:
        """
        Instead of A*, we use a Dynamic Window approach. 
        Generate a discrete set of legal velocity vectors we can physically execute.
        """
        speed = np.linalg.norm(current_vel)
        heading = np.arctan2(current_vel[1], current_vel[0])
        
        candidates = []
        # Option 1: Maintain current course and speed (preferred if safe)
        candidates.append(current_vel)
        
        # Option 2: Alter course to Starboard (Right) - COLREGS Rule 14/15 compliant
        right_heading = heading - np.radians(15) # 15 degree noticeable turn
        candidates.append(np.array([np.cos(right_heading) * speed, np.sin(right_heading) * speed]))
        
        # Option 3: Slow down (Negative acceleration) - COLREGS Rule 8 compliant
        slow_speed = speed * 0.5 
        candidates.append(np.array([np.cos(heading) * slow_speed, np.sin(heading) * slow_speed]))
        
        # Option 4: Port (Left) turn - heavily penalized later unless strictly necessary
        left_heading = heading + np.radians(15)
        candidates.append(np.array([np.cos(left_heading) * speed, np.sin(left_heading) * speed]))
        
        return candidates

    def evaluate_cost(self, maneuver: np.ndarray, own_ship: Ship, targets: List[Ship], whales: List[Whale], intended_waypoint: np.ndarray) -> float:
        """
        The Cost Function: Lower score is better.
        """
        cost = 0.0
        
        # 1. Cross-Track / Waypoint Cost (Keep us moving toward the goal)
        projected_pos = own_ship.pos + (maneuver * 300) # Project 5 mins (300s) ahead
        dist_to_goal = np.linalg.norm(intended_waypoint - projected_pos)
        cost += dist_to_goal * 0.1 
        
        # Create a hypothetical ship using this maneuver to test CPA
        test_ship = Ship("test", Vector2D(*own_ship.pos), Vector2D(*maneuver), True)
        
        # 2. Ship Collision Cost (COLREGS)
        for target in targets:
            tcpa, cpa = calculate_cpa(test_ship, target)
            
            if tcpa > 0 and tcpa < 600: # If approaching within 10 minutes
                if cpa < self.safe_ship_distance:
                    # Exponential penalty for getting too close
                    cost += 10000.0 / (cpa + 1.0)
                
                # COLREGS penalty: If we turned left (port) to avoid a head-on, penalize it!
                # We want the algorithm to naturally select the starboard turn candidate.
                angle_diff = np.arctan2(maneuver[1], maneuver[0]) - np.arctan2(own_ship.vel[1], own_ship.vel[0])
                if angle_diff > 0: # Port turn
                    cost += 500.0

        # 3. Whale Collision Cost (Probabilistic Uncertainty)
        for whale in whales:
            # We don't use strict CPA for whales because they move unpredictably.
            # Instead, we check if our projected path cuts through their growing uncertainty cloud.
            dist_to_whale = np.linalg.norm(whale.pos - projected_pos)
            hazard_radius = whale.get_uncertainty_radius()
            
            if dist_to_whale < hazard_radius:
                cost += 8000.0 # High penalty for entering a whale zone
                
        return cost

    def update_loop(self, own_ship: Ship, targets: List[Ship], whales: List[Whale], waypoint: np.ndarray):
        """
        The main simulation tick. Run this every X seconds or when new AIS data arrives.
        """
        # Step 1: Generate possible actions
        candidates = self.generate_candidate_maneuvers(own_ship.vel)
        
        # Step 2: Grade every action using the cost function
        best_maneuver = None
        lowest_cost = float('inf')
        
        for maneuver in candidates:
            cost = self.evaluate_cost(maneuver, own_ship, targets, whales, waypoint)
            
            # Hysteresis (Anti-Thrashing):
            # If we already have an active command (like "turning right"), we apply 
            # a slight discount to continuing that command so the ship doesn't twitch back and forth.
            if self.active_command is not None and np.array_equal(maneuver, self.active_command):
                cost *= 0.9 
                
            if cost < lowest_cost:
                lowest_cost = cost
                best_maneuver = maneuver
                
        # Step 3: Execute the safest legal maneuver
        self.active_command = best_maneuver
        return best_maneuver