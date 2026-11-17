import { createApp } from "vue";
import App from "./App.vue";
import ProductionApp from "./ProductionApp.vue";
import "./styles.css";
import "./research-workbench.css";
import "./run-pages.css";

const productionRoutes = ["/production", "/publication-runs", "/published"];
createApp(productionRoutes.includes(window.location.pathname) ? ProductionApp : App).mount("#generation-app");
