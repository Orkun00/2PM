//
// Created by Orkun Acar on 19.08.2026.
//

#ifndef GALVO_CONTROLLER_H
#define GALVO_CONTROLLER_H

class Galvo {
    public:
        [[nodiscard]] bool set_voltage_xy(double input_voltage_x, double input_voltage_y);
        [[nodiscard]] bool set_voltage_x(double input_voltage_x);
        [[nodiscard]] bool set_voltage_y(double input_voltage_y);

        double get_voltage_x() const; // NOLINT(*-use-nodiscard)
        double get_voltage_y() const; // NOLINT(*-use-nodiscard)
        double get_degree_x() const; // NOLINT(*-use-nodiscard)
        double get_degree_y() const; // NOLINT(*-use-nodiscard)
        static constexpr double get_voltage_limit() {
            return VOLTAGE_LIMIT;
        }
        static constexpr double get_angle_limit_deg() {
            return ANGLE_LIMIT_DEG;
        }
        static constexpr double degree_to_voltage(double input_degree) {
            return (input_degree / ANGLE_LIMIT_DEG) * VOLTAGE_LIMIT;
        }
        static constexpr double voltage_to_degree(double input_voltage) {
            return (input_voltage / VOLTAGE_LIMIT) * ANGLE_LIMIT_DEG;
        }

    private:
        static constexpr double VOLTAGE_LIMIT = 5.0;
        static constexpr double ANGLE_LIMIT_DEG = 22.5;
        double voltage_x{};
        double voltage_y{};
};
#endif //GALVO_CONTROLLER_H
